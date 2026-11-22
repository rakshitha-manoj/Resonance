#!/usr/bin/env python
"""Interactive digital twin of the Resonance 3-node system for the viva demo (SIMULATION ONLY).

A simulated 10-storey building (data/generator/building_sim.py, the demo building: seed 5,990,011, f1 7.09 Hz, sensors on floors 5 and
10) is monitored by the REAL firmware logic (firmware/esp32 netsim: master + 2 slaves, rule f, f1 tracker, DAMAGE channel). Buttons on the
dashboard send commands; the twin changes the simulated building (damage in a storey, an impact, a shake) or the network (kill / revive a
sensor) from a few superframes in the future on, re-runs netsim over the whole history and publishes the new lines in real time with the
topics of tools/sim_publisher.py. EVERY decision (events, DAMAGE, DEAD, degraded mode) comes from the firmware C++ code; this file only
generates the sensor signals and replays the netsim log.

    .venv\\Scripts\\python.exe tools\\twin_server.py --speed 2 --transport websockets --port 8000        (site id is printed)

Topics (resonance/<site>/...): the usual line topics (meta, sample, begin, change, damage, node, ..., state, status, damage_state) plus
    twin/state  retained JSON: running, speed, building f1, current damage, true f1 now, expected drop, killed sensors, truth history, log tail
    twin/log    one human-readable line per applied command
    twin/cmd    commands (JSON, from the dashboard):
        {"cmd":"damage","storey":1..10,"loss":0.05..0.5}   {"cmd":"repair"}   {"cmd":"impact","floor":5|10}   {"cmd":"shake"}
        {"cmd":"kill","slave":1|2}   {"cmd":"revive","slave":1|2}   {"cmd":"reset"}
Anybody who knows the (random) site id on a public broker can send commands: acceptable for a simulated demo, never for a real plant.
Commands are validated strictly (non-finite numbers, wrong types, extra keys, > 256 bytes: ignored), rate limited AT RECEIVE TIME (1 per 2 s,
the first command of a window wins and at most ONE command is pending, so a flood costs nothing), refused while the system commissions,
and spaced at least MIN_SPACING_SF = 6 superframes apart in simulated time (otherwise two quick clicks, e.g. kill then revive, would land
in adjacent superframes and the first effect would never be visible). A command takes effect GUARD superframes after the one being
published (the already published past never changes: the netsim output is deterministic and the published prefix is verified). The
netsim re-run after a command blocks publishing for about 1-3 s (longer late in a long session); with the limits above that is at
most one such pause per 2 s of wall time, and the pacer then catches up, so the stream is never frozen for more than a few seconds.
A retained message published to twin/cmd IS applied once when it arrives live (the broker delivers a live publish with retain=0); only
the replay of a retained message on a (re)subscribe, which has retain=1, is ignored.
twin/state carries `updated_t` (wall-clock seconds) and `period_s` (republished every period_s): a reader should treat a state older than
about 3 x period_s as a dead twin (a killed server cannot publish running:false; Ctrl+C, SIGTERM and Ctrl+Break do).
"""
import argparse
import dataclasses
import json
import math
import os
import queue
import re
import signal
import random
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "data", "generator"))
sys.dont_write_bytecode = True
import building_sim as bs  # noqa: E402
import resonance_mqtt as rm  # noqa: E402

FW = os.path.join(REPO, "firmware", "esp32")
DEFAULT_EXE = os.path.join(FW, ".pio", "build", "netsim", "program.exe" if os.name == "nt" else "program")
DEFAULT_SEED = 5_990_011      # the demo building of gen_traces.py: f1 7.09 Hz, a storey-1 loss of 30 % moves it by 3.9 %
FLOORS = (5, 10)
SF_S = 1.4                    # superframe length (net_proto.h)
COMMISSION_SF = 100           # AE ready at window 21, f1 commissioned at window ~95 on both slaves: commands wait until here
GUARD_SF = 4                  # a command acts from (published superframe + GUARD_SF) on
LOOKAHEAD_SF = 150            # netsim is kept this far ahead of the published line
EXTEND_WHEN_SF = 60           # extend the timeline when fewer than this many superframes are left unpublished
CHUNK = 40                    # plain ambient pieces are at most this many windows
TRANSIENT_WINDOWS = 12        # impact / shake last this many superframes (16.8 s)
SHAKE_AMP = 3.0
SHAKE_EXC = 16                # exc ids of gen_traces.py known to give a clean event (see IMPACT_EXC for the impacts)
IMPACT_EXC = {5: 2, 10: 2}
PLAIN_EXC0 = 1000             # ambient pieces use fresh exc ids from here (disjoint from the scripted ones)
RATE_LIMIT_S = 2.0
MIN_SPACING_SF = 6            # applied commands are at least this many superframes apart (simulated time)
STATE_PERIOD_S = 5.0          # twin/state republish period (heartbeat: updated_t)
MAX_LOG = 12
MAX_TRUTH = 200               # truth history kept (the first entry and the last MAX_TRUTH-1)
MAX_CMD_BYTES = 256
LOSS_MIN, LOSS_MAX = 0.05, 0.5

CMD_FIELDS = {"damage": {"storey", "loss"}, "repair": set(), "impact": {"floor"}, "shake": set(), "kill": {"slave"}, "revive": {"slave"}, "reset": set()}


def _num(v):
    """Finite JSON number -> float/int, else None (bool, str, NaN, +-inf, huge ints are not numbers here)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return v if math.isfinite(v) else None
    except OverflowError:                      # an int too large for a float
        return None


def _int_in(v, lo, hi=None, allowed=None):
    v = _num(v)
    if v is None:
        return None
    if isinstance(v, float):
        if not v.is_integer():
            return None
        v = int(v)
    if allowed is not None:
        return v if v in allowed else None
    return v if lo <= v <= hi else None


def _no_const(name):
    raise ValueError("non-finite constant " + name)


def parse_command(payload):
    """bytes/str -> validated command dict {"cmd":..., fields...} or None (anything wrong, unknown, oversized, non-finite, extra keys, bad
    types). Never raises."""
    try:
        return _parse_command(payload)
    except Exception:                          # a hostile payload must never take the server down
        return None


def _parse_command(payload):
    if isinstance(payload, str):
        payload = payload.encode("utf-8", "replace")
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_CMD_BYTES:
        return None
    o = json.loads(payload.decode("utf-8"), parse_constant=_no_const)
    if not isinstance(o, dict):
        return None
    c = o.get("cmd")
    if not isinstance(c, str) or c not in CMD_FIELDS or set(o) - {"cmd"} != CMD_FIELDS[c]:
        return None
    if c == "damage":
        s = _int_in(o["storey"], 1, 10)
        l = _num(o["loss"])
        if s is None or l is None or not (LOSS_MIN - 1e-9 <= l <= LOSS_MAX + 1e-9):
            return None
        return {"cmd": c, "storey": s, "loss": round(float(l), 4)}
    if c == "impact":
        f = _int_in(o["floor"], 0, 0, allowed=FLOORS)
        return None if f is None else {"cmd": c, "floor": f}
    if c in ("kill", "revive"):
        s = _int_in(o["slave"], 0, 0, allowed=(1, 2))
        return None if s is None else {"cmd": c, "slave": s}
    return {"cmd": c}


class RateLimiter:
    """At most one command per `period` seconds (monotonic clock)."""

    def __init__(self, period=RATE_LIMIT_S, clock=time.monotonic):
        self.period, self.clock, self.last = period, clock, None

    def allow(self):
        now = self.clock()
        if self.last is not None and now - self.last < self.period:
            return False
        self.last = now
        return True


@dataclasses.dataclass(frozen=True)
class Seg:
    """`n` windows of a recording rendered with `n_render` windows (n < n_render for a piece that was cut short; the PREFIX of the
    rendering is kept, so history never changes)."""
    start: int
    n: int
    n_render: int
    exc: int
    amp: float
    impact_floor: object
    damage: object

    def key(self):
        return (self.n_render, self.exc, self.amp, self.impact_floor, self.damage)


class Timeline:
    """Sensor windows of both slaves, built lazily in pieces; the part before a change is frozen, the future is rebuilt."""

    def __init__(self, seed):
        self.p = bs.building_params(seed)
        self.pieces = []
        self.cache = {}
        self.next_exc = PLAIN_EXC0

    def end(self):
        return self.pieces[-1].start + self.pieces[-1].n if self.pieces else 0

    def truncate(self, g):
        new = []
        for s in self.pieces:
            if s.start >= g:
                break
            new.append(dataclasses.replace(s, n=g - s.start) if s.start + s.n > g else s)
        self.pieces = new
        assert self.end() <= g

    def fill(self, g, n_total, damage, overlays):
        """Append pieces from g (== end of the frozen prefix) to n_total windows under the CURRENT state: damage (storey, loss) | None and
        overlays [{kind: impact|shake, floor, end}]."""
        assert self.end() == g or (not self.pieces and g == 0), (self.end(), g)
        pos = g
        while pos < n_total:
            act = [o for o in overlays if o["end"] > pos]
            bp = min([pos + CHUNK] + [o["end"] for o in act])
            bp = min(bp, n_total) if not act else bp
            imp = [o for o in act if o["kind"] == "impact"]
            shk = [o for o in act if o["kind"] == "shake"]
            floor = imp[-1]["floor"] if imp else None
            if floor is not None:
                exc = IMPACT_EXC[floor]
            elif shk:
                exc = SHAKE_EXC
            else:
                exc = self.next_exc
                self.next_exc += 1
            n = bp - pos
            self.pieces.append(Seg(pos, n, n, exc, SHAKE_AMP if shk else 1.0, floor, damage))
            pos = bp

    def render(self, s):
        k = s.key()
        if k not in self.cache:
            sig = bs.simulate_batch([self.p], s.exc, s.n_render, [s.damage], "default", s.amp)[0]
            az, gx = bs.sensor_output(self.p, sig, s.exc, impact_floor=s.impact_floor)
            self.cache[k] = (np.stack([bs.to_windows(az[f - 1]) for f in FLOORS]), np.stack([bs.to_windows(gx[f - 1]) for f in FLOORS]))
        return self.cache[k]

    def arrays(self, n):
        """(2, n, 256) int16 az|gx windows of the first n windows."""
        parts = []
        for s in self.pieces:
            if s.start >= n:
                break
            az, gx = self.render(s)
            m = min(s.n, n - s.start)
            parts.append(np.concatenate([az[:, :m], gx[:, :m]], axis=2))
        out = np.concatenate(parts, axis=1)
        assert out.shape[1] == n and out.shape[2] == 256, out.shape
        return out.astype("<i2")


def write_traces(path, arr):
    import struct
    n = arr.shape[1]
    with open(path, "wb") as f:
        f.write(b"RTRC" + struct.pack("<III", 1, 2, n))
        for i in range(2):
            f.write(np.ascontiguousarray(arr[i]).astype("<i2").tobytes())
    with open(path + ".floors", "w") as f:
        f.write("%d,%d\n" % FLOORS)


def line_field(line, key):
    try:
        return json.loads(line).get(key)
    except ValueError:
        return None


SAMPLE_RE = re.compile(r'^\{"type":"sample","t_us":(\d+),"sf":(\d+)')


class TwinCore:
    """The twin without MQTT and without real-time pacing: state, timeline, netsim runs, prefix verification."""

    def __init__(self, seed=DEFAULT_SEED, workdir=None, exe=DEFAULT_EXE, commission_sf=COMMISSION_SF, guard=GUARD_SF, lookahead=LOOKAHEAD_SF, warn=None):
        self.seed, self.exe = seed, exe
        self.commission_sf, self.guard, self.lookahead = commission_sf, guard, lookahead
        self.tl = Timeline(seed)
        self.p = self.tl.p
        self.f1_healthy = bs.f1_hz(self.p)
        self.damage = None
        self.killed = set()
        self.overlays = []
        self.kills, self.revives = [], []          # (node, sf), node 1|2 = slave
        self.truth = [(0, self.f1_healthy)]        # (sf from which it holds, true f1 Hz)
        self.last_g = -1
        self.cur_sf = 0                            # superframe of the last published line
        self.lines = []                            # current netsim output
        self.n_pub = 0                             # lines published so far (their text is lines[:n_pub] by construction)
        self.pub_head = []                         # published lines (kept for verification)
        self.log = []
        self.last_cmd = None
        self.changed = False
        self.sf_t = {}
        self.warnings = []
        self.warn = warn or (lambda m: print("twin warning: " + m, file=sys.stderr, flush=True))
        self.workdir = os.path.abspath(workdir or tempfile.mkdtemp(prefix="twin_"))
        os.makedirs(self.workdir, exist_ok=True)
        self.runs = 0
        self.tl.fill(0, 0, None, [])
        self.refresh()

    # ---- state ----
    def commissioned(self):
        return self.cur_sf >= self.commission_sf

    def f1_true_now(self):
        return bs.f1_hz(self.p, self.damage)

    def drop_true(self):
        return (self.f1_healthy - self.f1_true_now()) / self.f1_healthy

    def sf_to_t_us(self, sf):
        return self.sf_t.get(sf)

    def _index_t(self):
        self.sf_t = {}
        for ln in self.lines:
            m = SAMPLE_RE.match(ln)
            if m:
                self.sf_t[int(m.group(2))] = int(m.group(1))

    def _logadd(self, m):
        self.log.append(m)
        del self.log[:-MAX_LOG]

    # ---- netsim ----
    def _netsim(self, n_sf):
        arr = self.tl.arrays(n_sf)
        tr = os.path.join(self.workdir, "traces.bin")
        out = os.path.join(self.workdir, "events.jsonl")
        write_traces(tr, arr)
        if os.path.exists(out):
            os.remove(out)
        cmd = [self.exe, "--traces", tr, "--events-jsonl", out, "--quiet", "--superframes", str(n_sf)]
        for node, sf in self.kills:
            cmd += ["--kill", "%d:%d" % (node, sf)]
        for node, sf in self.revives:
            cmd += ["--revive", "%d:%d" % (node, sf)]
        r = subprocess.run(cmd, cwd=FW, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("netsim failed (%d): %s" % (r.returncode, (r.stdout + r.stderr)[-400:]))
        self.runs += 1
        with open(out, "r", encoding="utf-8", newline="") as f:
            return [ln.rstrip("\r\n") for ln in f if ln.strip()]

    def refresh(self):
        """Fill the timeline up to cur_sf + lookahead (the future under the current state), re-run netsim and verify that the lines
        already published are unchanged. Returns True if the published prefix is byte-identical."""
        n_sf = self.cur_sf + self.lookahead
        if self.tl.end() < n_sf:
            self.tl.fill(self.tl.end(), n_sf, self.damage, self.overlays)
        new = self._netsim(self.tl.end())
        ok = new[:self.n_pub] == self.pub_head
        if not ok:
            bad = next((i for i, (a, b) in enumerate(zip(new, self.pub_head)) if a != b), min(len(new), len(self.pub_head)))
            self.warn("published prefix changed at line %d (of %d published); continuing after the same t_us" % (bad, self.n_pub))
            self.warnings.append(bad)
            last_t = line_field(self.pub_head[-1], "t_us") if self.pub_head else None
            if last_t is not None:
                k = 0
                while k < len(new) and (line_field(new[k], "t_us") is None or line_field(new[k], "t_us") <= last_t):
                    k += 1
                self.n_pub = k
            self.pub_head = new[:self.n_pub]
        self.lines = new
        self._index_t()
        return ok

    def peek(self):
        """The next unpublished line or None."""
        return self.lines[self.n_pub] if self.n_pub < len(self.lines) else None

    def next_lines(self):
        """Unpublished lines (list), without marking them."""
        return self.lines[self.n_pub:]

    def mark_published(self, line):
        """Call for every line actually published, in order."""
        self.pub_head.append(line)
        self.n_pub = len(self.pub_head)
        sf = line_field(line, "sf")
        if isinstance(sf, int) and sf > self.cur_sf:
            self.cur_sf = sf

    def need_extend(self):
        last = max((line_field(self.lines[i], "sf") or 0 for i in range(max(0, len(self.lines) - 5), len(self.lines))), default=0)
        return last - self.cur_sf < EXTEND_WHEN_SF

    # ---- commands ----
    def _g(self):
        g = max(self.cur_sf + self.guard, self.last_g + 1)
        self.last_g = g
        return g

    def _msg(self, g, text):
        m = "sf %d: %s" % (g, text)
        self._logadd(m)
        return m

    def _set_truth(self, g):
        f = self.f1_true_now()
        if self.truth and self.truth[-1][0] == g:
            self.truth[-1] = (g, f)
        else:
            self.truth.append((g, f))
            if len(self.truth) > MAX_TRUTH:
                del self.truth[1]

    def apply(self, c, refresh=True):
        """Apply a VALIDATED command; returns the log line. Rebuilds the future; with refresh=False the caller must call refresh()
        (netsim re-run, about a second) before publishing further lines, so it can send the log line first."""
        self.changed = False
        cmd = c["cmd"]
        if not self.commissioned():
            m = "sf %d: command '%s' ignored: the system is still commissioning (f1 needs about %d superframes, %.0f s at 1x)" % (self.cur_sf, cmd, self.commission_sf, self.commission_sf * SF_S)
            self._logadd(m)
            return m
        if (cmd == "kill" and c["slave"] in self.killed) or (cmd == "revive" and c["slave"] not in self.killed):
            m = "sf %d: sensor %d is %s, nothing done" % (self.cur_sf, c["slave"], "already dead" if cmd == "kill" else "not dead")
            self._logadd(m)
            return m
        if self.last_g >= 0 and self.cur_sf + self.guard < self.last_g + MIN_SPACING_SF:
            wait = self.last_g + MIN_SPACING_SF - self.cur_sf - self.guard
            m = "sf %d: command '%s' ignored: too soon after the previous one (applied at sf %d); wait %d more superframes (about %.0f s at 1x)" % (self.cur_sf, cmd, self.last_g, wait, wait * SF_S)
            self._logadd(m)
            return m
        g = self._g()
        self.changed = True
        before = self.f1_true_now()
        pct = lambda a, b: 100.0 * (a - b) / a
        if cmd == "damage":
            self.damage = (c["storey"], c["loss"])
            self._set_truth(g)
            after = self.f1_true_now()
            msg = "damage storey %d by %.0f %% applied; true f1 %.2f -> %.2f Hz (-%.1f %% vs healthy %.2f Hz)%s" % (
                c["storey"], 100 * c["loss"], before, after, pct(self.f1_healthy, after), self.f1_healthy,
                "" if pct(self.f1_healthy, after) >= 2.0 else "; below the 2 % DAMAGE threshold, so no alarm is expected")
        elif cmd == "repair":
            self.damage = None
            self._set_truth(g)
            msg = "repair applied; true f1 %.2f -> %.2f Hz" % (before, self.f1_true_now())
        elif cmd == "impact":
            self.overlays = [o for o in self.overlays if o["kind"] != "impact" and o["end"] > g] + [{"kind": "impact", "floor": c["floor"], "end": g + TRANSIENT_WINDOWS}]
            msg = "impact on floor %d (sensor %d) for %d superframes" % (c["floor"], FLOORS.index(c["floor"]) + 1, TRANSIENT_WINDOWS)
        elif cmd == "shake":
            self.overlays = [o for o in self.overlays if o["kind"] != "shake" and o["end"] > g] + [{"kind": "shake", "floor": None, "end": g + TRANSIENT_WINDOWS}]
            msg = "building-wide shake (%.0fx) for %d superframes" % (SHAKE_AMP, TRANSIENT_WINDOWS)
        elif cmd == "kill":
            s = c["slave"]
            self.killed.add(s)
            self.kills.append((s, g))
            msg = "sensor %d killed (power loss); DEAD after 3 missed superframes, then degraded mode" % s
        elif cmd == "revive":
            s = c["slave"]
            self.killed.discard(s)
            self.revives.append((s, g))
            msg = "sensor %d revived; it recommissions its autoencoder and f1 (about 2 min) on the CURRENT structure" % s
        elif cmd == "reset":
            self.damage = None
            self.overlays = []
            for s in sorted(self.killed):
                self.revives.append((s, g))
            self.killed.clear()
            self._set_truth(g)
            msg = "reset: healthy structure, all sensors alive, effects cancelled (baselines are kept; a revived sensor recommissions)"
        else:
            raise ValueError(cmd)
        self.tl.truncate(g)
        self.tl.fill(g, max(self.tl.end(), self.cur_sf + self.lookahead, g + 1), self.damage, self.overlays)
        self.last_cmd = {"cmd": cmd, "sf": g, **{k: v for k, v in c.items() if k != "cmd"}}
        out = self._msg(g, msg)
        if refresh:
            self.refresh()
        return out

    def state(self, speed, running=True):
        d = self.damage
        truth = []
        for sf, f in self.truth:
            t = self.sf_to_t_us(sf)
            truth.append([sf, t, round(f, 5)])
        return {
            "running": running, "speed": speed, "seed": self.seed, "sf": self.cur_sf,
            "commissioned": self.commissioned(), "commission_sf": self.commission_sf,
            "building": {"f1_hz": round(self.f1_healthy, 5), "floors": list(FLOORS)},
            "damage": None if d is None else {"storey": d[0], "loss": d[1]},
            "f1_true_hz": round(self.f1_true_now(), 5), "drop_true": round(self.drop_true(), 5),
            "killed": sorted(self.killed), "truth": truth, "last_cmd": self.last_cmd, "log": self.log[-8:], "guard_sf": self.guard,
        }


# --------------------------------------------------------------------------------------------- MQTT front end
class TwinServer:
    def __init__(self, args):
        import sim_publisher as sp
        self.args = args
        self.speed = args.speed
        os.makedirs(args.workdir, exist_ok=True)
        self.core = TwinCore(seed=args.seed, workdir=args.workdir, exe=args.netsim_exe)
        self.pub = sp.Publisher(args)
        self.pacer = sp.Pacer(args.speed)
        self.cmds = queue.Queue(maxsize=1)        # at most ONE pending command; the first one of a rate-limit window wins
        self.notices = queue.Queue(maxsize=4)     # rate-limit / busy notices from the MQTT thread, logged by the main loop
        self.limiter = RateLimiter()              # applied at RECEIVE time (on_message), not when the command is dequeued
        self.reject_limiter = RateLimiter()
        self.cmd_topic = rm.topic(args.site, "twin/cmd")
        self.stop = False
        self.last_state = 0.0

    def _publish(self, leaf, payload, retain=False):
        t = rm.topic(self.args.site, leaf)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.pub.client.publish(t, payload, qos=1, retain=retain)

    def publish_state(self, running=True):
        st = self.core.state(self.speed, running)
        st["updated_t"] = round(time.time(), 1)     # heartbeat: a reader treats a state older than ~3 x period_s as a dead twin
        st["period_s"] = STATE_PERIOD_S
        self._publish("twin/state", json.dumps(st, separators=(",", ":")), True)
        self.last_state = time.monotonic()

    def publish_log(self, text):
        rm.log("[twin] " + text)
        self._publish("twin/log", text, False)

    def on_message(self, client, ud, msg):
        try:
            # a retained message REPLAYED on (re)subscribe (retain=1) is never applied; a live publish arrives with retain=0 even if the
            # sender set the retain flag, and is applied like any other command
            if msg.topic != self.cmd_topic or msg.retain:
                return
            c = parse_command(bytes(msg.payload))
            if c is None:
                return                                    # unknown / malformed: ignored silently
            if not self.limiter.allow():
                self._notice("command '%s' ignored: rate limit (1 per %.0f s)" % (c["cmd"], RATE_LIMIT_S))
                return
            try:
                self.cmds.put_nowait(c)
            except queue.Full:
                self._notice("command '%s' ignored: busy with the previous command" % c["cmd"])
        except Exception as e:                            # never raise into paho's thread
            rm.log("on_message error: %r" % (e,))

    def _notice(self, text):
        if self.reject_limiter.allow():
            try:
                self.notices.put_nowait(text)
            except queue.Full:
                pass

    def start(self):
        self.pub.connect()
        c = self.pub.client
        orig = c.on_connect

        def on_connect(cl, ud, flags, rc, props=None):
            orig(cl, ud, flags, rc, props)
            cl.subscribe(self.cmd_topic, qos=1)
        c.on_connect = on_connect
        c.on_message = self.on_message
        c.subscribe(self.cmd_topic, qos=1)
        self.publish_state()

    def handle(self, c):
        try:
            msg = self.core.apply(c, refresh=False)
            self.publish_log(msg)          # the log line goes out before the (slower) netsim re-run
            if self.core.changed:          # refused / no-op commands cost no netsim run
                self.core.refresh()
            self.publish_state()
        except Exception as e:
            rm.log("command %r failed: %r" % (c, e))
            try:
                self.publish_log("sf %d: internal error while applying '%s'; the twin keeps running" % (self.core.cur_sf, c.get("cmd")))
            except Exception:
                pass

    def run(self):
        core = self.core
        was_comm = False
        while not self.stop:
            try:
                self.publish_log("sf %d: %s" % (core.cur_sf, self.notices.get_nowait()))
            except queue.Empty:
                pass
            try:
                self.handle(self.cmds.get_nowait())       # one command per loop pass, then the stream continues
            except queue.Empty:
                pass
            if core.need_extend():
                try:
                    core.refresh()
                except Exception as e:
                    rm.log("timeline extension failed: %r" % (e,))
                    time.sleep(1.0)
            line = core.peek()
            if line is not None:
                w = self.pacer.due(rm_t_us(line))
                if w > 0:
                    time.sleep(min(w, 0.03))
                    continue
                self.pub.send_line(line)
                core.mark_published(line)
                if core.commissioned() != was_comm:
                    was_comm = core.commissioned()
                    self.publish_state()
                    if was_comm:
                        self.publish_log("sf %d: commissioning complete, commands enabled" % core.cur_sf)
            else:
                time.sleep(0.03)
            if time.monotonic() - self.last_state > STATE_PERIOD_S:
                self.publish_state()

    def shutdown(self):
        try:
            self.publish_state(running=False)
        except Exception as e:
            rm.log("final state not published: %r" % (e,))
        try:
            self.pub.finish()
        except Exception as e:
            rm.log("shutdown: %r" % (e,))


def rm_t_us(line):
    t = line_field(line, "t_us")
    return t if isinstance(t, int) else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    rm.add_connection_args(ap)
    ap.add_argument("--speed", type=float, default=2.0, help="1 = real time (1.4 s per superframe); commissioning needs about %d superframes = %.1f min at 1x. Default 2" % (COMMISSION_SF, COMMISSION_SF * SF_S / 60))
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="building seed (default: the demo building)")
    ap.add_argument("--qos", type=int, choices=(0, 1), default=1)
    ap.add_argument("--netsim-exe", default=DEFAULT_EXE)
    ap.add_argument("--workdir", default=None, help="scratch folder (default .cache/twin/<site>)")
    ap.set_defaults(site=None)          # None = not given (also for --site=foo): then env RESONANCE_SITE, else a NEW random id
    args = ap.parse_args()
    if args.site is None:
        args.site = os.environ.get("RESONANCE_SITE") or "twin-" + "".join(random.choice("0123456789abcdef") for _ in range(8))   # never the default demo site
    if not rm.site_ok(args.site):
        ap.error("bad --site (a-z 0-9 - _, max 32)")
    if not (0.1 <= args.speed <= 200):
        ap.error("--speed must be between 0.1 and 200")
    if not os.path.exists(args.netsim_exe):
        raise SystemExit("netsim not built: %s (run `pio run -e netsim` in firmware/esp32)" % args.netsim_exe)
    args.workdir = args.workdir or os.path.join(REPO, ".cache", "twin", args.site)
    srv = TwinServer(args)
    srv.start()
    rm.log("digital twin running: site %s, speed %gx, building f1 %.2f Hz. Dashboard: MQTT live with site '%s'. Commissioning takes about %.0f s of wall time; Ctrl+C to stop."
           % (args.site, args.speed, srv.core.f1_healthy, args.site, COMMISSION_SF * SF_S / args.speed))
    print("SITE=" + args.site, flush=True)
    def _stop(signum, frame):
        srv.stop = True
    for name in ("SIGTERM", "SIGBREAK"):    # SIGBREAK = Ctrl+Break on Windows
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _stop)
            except (ValueError, OSError):
                pass
    try:
        srv.run()
    except KeyboardInterrupt:
        rm.log("interrupted")
    srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
