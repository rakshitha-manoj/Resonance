#!/usr/bin/env python
"""Publishes Resonance JSON lines (schema 2, firmware/esp32/include/fusion_json.h) to MQTT, with the same topics as the
master firmware (resonance/<site>/<type>, see resonance_mqtt.py). Three sources:

  default   run the netsim simulator on the demo traces and publish its event log, paced by each line's t_us
              python tools/sim_publisher.py --speed 10
  --jsonl   publish an existing log (e.g. dashboard/demo_events_faults_sim.jsonl) instead of running netsim
  --serial  bridge a REAL master's serial port: forward every JSON line the firmware prints, in real time
              python tools/sim_publisher.py --serial COM5          (hardware day)

The demo fallback when Wokwi is not available. Pacing: a line with t_us is sent when its t_us has elapsed (divided by
--speed); meta is sent at once; when t_us goes backwards (a master restart) the timeline simply continues. --speed 0 =
as fast as the broker takes it. Default broker: the free public HiveMQ (broker.hivemq.com:1883), simulator data ONLY.
A private broker needs only options/env: --host --port 8883 --tls --user, env MQTT_PASSWORD. No secrets are stored.
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resonance_mqtt as rm  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FW = os.path.join(REPO, "firmware", "esp32")


def netsim_lines(exe, traces, extra):
    """Runs netsim into a temp file and returns its JSON lines (without newlines)."""
    if not os.path.exists(exe):
        raise SystemExit("netsim not built: %s (run `pio run -e netsim` in firmware/esp32)" % exe)
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "events.jsonl")
        cmd = [exe, "--traces", traces, "--events-jsonl", out, "--quiet"] + extra
        r = subprocess.run(cmd, cwd=FW, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            raise SystemExit("netsim failed (%d): %s" % (r.returncode, (r.stdout + r.stderr)[-400:]))
        return read_lines(out)


def read_lines(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return [ln.rstrip("\r\n") for ln in f if ln.strip()]


def serial_lines(port, baud):
    """Yields the JSON lines a master prints on its serial port (the CSV R,/E,/S, and # lines are skipped)."""
    try:
        import serial  # pyserial
    except ImportError:
        raise SystemExit("--serial needs pyserial: pip install pyserial")
    with serial.Serial(port, baud, timeout=1) as s:
        buf = b""
        while True:
            chunk = s.read(512)
            if not chunk:
                yield None  # idle tick so the caller can check for Ctrl-C / flush
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                ln = raw.decode("utf-8", "replace").rstrip("\r")
                if ln.startswith('{"type":'):
                    yield ln


class Pacer:
    """Maps a line's t_us to a wall-clock due time. t_us going backwards (restart) continues the timeline."""

    def __init__(self, speed, clock=time.monotonic):
        self.speed = speed
        self.clock = clock
        self.base_wall = None
        self.base_t = 0.0   # timeline offset in seconds (sim time already played before the last rebase)
        self.last_t = None
        self.offset = 0.0

    def due(self, t_us):
        """Seconds to wait before sending a line stamped t_us (0 if speed is 0 or it is already due)."""
        if not self.speed or t_us is None:
            return 0.0
        t = t_us / 1e6
        if self.last_t is not None and t < self.last_t:
            self.offset += self.last_t - t + 1.4  # restart: continue one superframe after the last line
        self.last_t = t
        sim = t + self.offset
        now = self.clock()
        if self.base_wall is None:
            self.base_wall = now - sim / self.speed
        return max(0.0, self.base_wall + sim / self.speed - now)


def lap_lines(lines, rng=random):
    """Lines of a repeat lap as a REBOOTED master would print them: every epoch of the lap is mapped to a fresh random one (the meta's,
    and those of netsim's own restart lines, keeping their prev_epoch chain), and the meta line is followed by a restart line (epoch =
    the new meta epoch, prev_epoch null: the host cannot tell a first boot from a reboot), exactly the firmware's boot pattern, which
    dashboard/tools/validate_logs.py accepts. Everything else is passed through byte for byte."""
    mapping = {}

    def fresh(old):
        if old not in mapping:
            mapping[old] = rng.randint(1, 2**32 - 1)
        return mapping[old]

    def remap(ln):
        return re.sub(r'"((?:prev_)?epoch)":(%sd+)' % chr(92), lambda m: '"%s":%d' % (m.group(1), fresh(int(m.group(2)))), ln)

    for ln in lines:
        k = rm.route(ln).kind
        if k == "meta":
            ln = remap(ln)
            yield ln
            yield '{"type":"restart","t_us":0,"epoch":%d,"prev_epoch":null}' % json.loads(ln)["epoch"]
        elif k == "restart":
            yield remap(ln)
        else:
            yield ln


def line_t_us(line):
    try:
        o = json.loads(line)
    except ValueError:
        return None
    t = o.get("t_us")
    return t if isinstance(t, int) else None


class Publisher:
    def __init__(self, args, epoch=None):
        self.args = args
        self.site = args.site
        self.epoch = epoch                 # boot epoch of the source (meta.epoch): part of the status payloads and the last will
        self.last_meta = None
        self.client = rm.make_client(args, will_site=self.site, will_epoch=epoch)
        self.client.max_inflight_messages_set(500)
        self.client.max_queued_messages_set(0)
        self.connected = False
        self.infos = []
        self.sent = 0
        self.by_topic = {}

    def connect(self):
        c = self.client

        def on_connect(cl, ud, flags, rc, props=None):
            # runs on the first connect AND after every automatic reconnect: a late subscriber or a restarted broker must see
            # the retained status and meta again (the will of the dropped session may have said "offline" meanwhile)
            ok = (rc == 0) if isinstance(rc, int) else not rc.is_failure
            if not ok:
                rm.log("connect refused: %s" % (rc,))
                return
            self._pub_status(True)
            if self.last_meta is not None:
                self._pub(rm.topic(self.site, "meta"), self.last_meta, True)
            self.connected = True

        c.on_connect = on_connect
        c.connect(self.args.host, self.args.port, keepalive=30)
        c.loop_start()
        t0 = time.time()
        while not self.connected and time.time() - t0 < 15:
            time.sleep(0.05)
        if not self.connected:
            raise SystemExit("cannot connect to %s:%d" % (self.args.host, self.args.port))
        rm.log("connected to %s:%d, site %s" % (self.args.host, self.args.port, self.site))

    def _pub(self, t, payload, retain):
        info = self.client.publish(t, payload, qos=self.args.qos, retain=retain)
        self.infos.append(info)
        if len(self.infos) > 5000:  # keep memory bounded; paho tracks the rest
            self.infos = self.infos[-1000:]
        return info

    def _pub_status(self, online):
        self._pub(rm.topic(self.site, "status"), rm.status_payload(online, self.epoch), True)

    def send_line(self, line):
        r = rm.route(line)
        payload = line.encode("utf-8")
        if r.kind == "meta":
            self.last_meta = payload
            try:
                ep = json.loads(line).get("epoch")
            except ValueError:
                ep = None
            if isinstance(ep, int) and ep != self.epoch:   # a new boot (serial mode): the status carries the new epoch
                self.epoch = ep
                self._pub_status(True)
        t = rm.topic(self.site, r.leaf)
        self._pub(t, payload, r.retain)
        if r.to_state:
            self._pub(rm.topic(self.site, "state"), payload, True)
        if r.to_damage_state:
            self._pub(rm.topic(self.site, "damage_state"), payload, True)
        self.sent += 1
        self.by_topic[r.leaf] = self.by_topic.get(r.leaf, 0) + 1

    def finish(self, offline=True):
        if offline:
            self._pub_status(False)
        for info in self.infos[-1000:]:
            info.wait_for_publish(timeout=30)
        self.client.loop_stop()
        self.client.disconnect()


def run(args):
    epoch = None
    if not args.serial:   # known up front: the status payloads and the last will carry meta.epoch
        first = read_lines(args.jsonl) if args.jsonl else None
        if first is None:
            first = netsim_lines(args.netsim_exe, args.traces, args.netsim_args.split() if args.netsim_args else [])
        for ln in first:
            if rm.route(ln).kind == "meta":
                try:
                    e = json.loads(ln).get("epoch")
                    epoch = e if isinstance(e, int) else None
                except ValueError:
                    pass
                break
    pub = Publisher(args, epoch)
    if args.dry_run:
        pub.connect = lambda: None
        pub._pub = lambda t, p, r: print("%s%s %s" % (t, " [retained]" if r else "", p.decode()[:110]))
        pub.finish = lambda offline=True: None
    pub.connect()
    pacer = Pacer(args.speed)
    try:
        n_run = 0
        while True:
            n_run += 1
            if args.serial:
                source = serial_lines(args.serial, args.baud)
                pacer = Pacer(0)  # the master already runs in real time
            elif args.jsonl:
                source = read_lines(args.jsonl)
            else:
                source = netsim_lines(args.netsim_exe, args.traces, args.netsim_args.split() if args.netsim_args else [])
            if n_run > 1 and not args.serial:
                source = lap_lines(source)   # a new lap = a rebooted master: new epoch in the meta, then a restart line, like the firmware
            for line in source:
                if line is None:
                    continue
                w = pacer.due(line_t_us(line))
                if w > 0:
                    time.sleep(w)
                pub.send_line(line)
                if args.max_lines and pub.sent >= args.max_lines:
                    break
            if args.max_lines and pub.sent >= args.max_lines:
                break
            if args.serial or not args.loop or (args.loop > 0 and n_run >= args.loop):
                break
            pacer = Pacer(args.speed)  # next lap: the netsim run starts again at t_us 0 (new meta line)
    except KeyboardInterrupt:
        rm.log("interrupted")
    pub.finish()
    rm.log("published %d lines: %s" % (pub.sent, ", ".join("%s=%d" % kv for kv in sorted(pub.by_topic.items()))))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    rm.add_connection_args(ap)
    s = ap.add_argument_group("source")
    s.add_argument("--jsonl", help="publish this JSONL log instead of running netsim")
    s.add_argument("--serial", metavar="COMx", help="bridge a real master's serial port (JSON lines only), real time")
    s.add_argument("--baud", type=int, default=115200)
    s.add_argument("--netsim-exe", default=os.path.join(FW, ".pio", "build", "netsim", "program.exe" if os.name == "nt" else "program"))
    s.add_argument("--traces", default=os.path.join("test", "data", "demo_traces.bin"), help="relative to firmware/esp32")
    s.add_argument("--netsim-args", default="", help='extra netsim arguments, e.g. "--superframes 170 --kill 2:30 --revive 2:62"')
    p = ap.add_argument_group("pacing")
    p.add_argument("--speed", type=float, default=1.0, help="1 = real time (1.4 s per superframe), 10 = ten times faster, 0 = as fast as possible")
    p.add_argument("--loop", type=int, default=0, metavar="N", help="repeat the run N times (-1 = forever); default once")
    p.add_argument("--max-lines", type=int, default=0, help="stop after this many lines (0 = all)")
    p.add_argument("--qos", type=int, choices=(0, 1), default=1)
    p.add_argument("--dry-run", action="store_true", help="print topics and payloads instead of publishing")
    args = ap.parse_args()
    if not rm.site_ok(args.site):
        ap.error("bad --site (a-z 0-9 - _, max 32)")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
