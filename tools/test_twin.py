"""Offline tests of tools/twin_server.py (no MQTT; needs the netsim build: `pio run -e netsim` in firmware/esp32).
Run: .venv\\Scripts\\python.exe tools\\test_twin.py"""
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import twin_server as ts  # noqa: E402

N = 0


def check(c, msg):
    global N
    N += 1
    if not c:
        raise AssertionError(msg)
    print("ok  %s" % msg)


def pub_until(core, sf):
    while core.next_lines():
        ln = core.next_lines()[0]
        s = ts.line_field(ln, "sf")
        if isinstance(s, int) and s > sf:
            break
        core.mark_published(ln)
        if core.need_extend():
            core.refresh()


def find(core, typ, **kw):
    out = []
    for ln in core.lines:
        o = json.loads(ln)
        if o.get("type") == typ and all(o.get(k) == v for k, v in kw.items()):
            out.append(o)
    return out


def test_commands():
    p = ts.parse_command
    check(p(b'{"cmd":"damage","storey":3,"loss":0.3}') == {"cmd": "damage", "storey": 3, "loss": 0.3}, "damage parsed")
    check(p('{"cmd":"repair"}') == {"cmd": "repair"} and p(b'{"cmd":"reset"}') and p(b'{"cmd":"shake"}'), "repair/reset/shake parsed")
    check(p(b'{"cmd":"impact","floor":10}') == {"cmd": "impact", "floor": 10} and p(b'{"cmd":"impact","floor":5}'), "impact floors 5 and 10")
    check(p(b'{"cmd":"kill","slave":2}') and p(b'{"cmd":"revive","slave":1}'), "kill/revive parsed")
    bad = [b'{"cmd":"damage","storey":0,"loss":0.3}', b'{"cmd":"damage","storey":11,"loss":0.3}', b'{"cmd":"damage","storey":1,"loss":0.9}',
           b'{"cmd":"damage","storey":1,"loss":0.01}', b'{"cmd":"damage","storey":true,"loss":0.3}', b'{"cmd":"damage","storey":1}',
           b'{"cmd":"damage","storey":1,"loss":0.3,"x":1}', b'{"cmd":"impact","floor":7}', b'{"cmd":"kill","slave":3}', b'{"cmd":"kill"}',
           b'{"cmd":"repair","x":1}', b'{"cmd":"explode"}', b'[]', b'nope', b'{"cmd":1}', b'\xff\xfe', b'{"cmd":"damage","storey":1,"loss":NaN}',
           b'{"cmd":"reset","pad":"' + b"a" * 300 + b'"}']
    check(all(p(b) is None for b in bad), "%d malformed / out-of-range / unknown commands rejected" % len(bad))
    t = [0.0]
    rl = ts.RateLimiter(2.0, clock=lambda: t[0])
    r = [rl.allow()]
    t[0] = 1.9
    r.append(rl.allow())
    t[0] = 2.0
    r.append(rl.allow())
    t[0] = 3.9
    r.append(rl.allow())
    check(r == [True, False, True, False], "rate limit 1 per 2 s")


def test_timeline(wd):
    a = ts.Timeline(ts.DEFAULT_SEED)
    a.fill(0, 30, None, [])
    b = ts.Timeline(ts.DEFAULT_SEED)
    b.fill(0, 30, None, [])
    check(np.array_equal(a.arrays(30), b.arrays(30)), "segment generation is deterministic")
    k = a.pieces[0].key()
    first = a.cache[k] if k in a.cache else None
    a.arrays(30)
    check(a.cache[k] is a.cache[k] and (first is None or a.cache[k] is first), "rendered pieces are cached")
    before = a.arrays(30)[:, :20].copy()
    a.truncate(20)
    a.fill(20, 40, (1, 0.3), [])
    after = a.arrays(40)
    check(np.array_equal(before, after[:, :20]), "history before a change is byte-identical after the future is rebuilt")
    check(not np.array_equal(a.arrays(40)[:, 20:30], b.arrays(30)[:, 20:30]), "the future differs after a damage change")


def test_scenario(wd):
    core = ts.TwinCore(workdir=wd)
    check(core.apply({"cmd": "damage", "storey": 1, "loss": 0.3}).find("commissioning") > 0, "command during commissioning is refused")
    pub_until(core, 130)
    check(core.commissioned(), "commissioned by sf 130")
    msg = core.apply({"cmd": "damage", "storey": 1, "loss": 0.3})
    g = core.last_cmd["sf"]
    check(g >= 130 + core.guard and "6.82" in msg and "7.09" in msg, "log line names effective sf and true f1: " + msg)
    pub_until(core, g + 90)
    begins = [d for d in find(core, "damage", state="BEGIN")]
    check(len(begins) == 1 and begins[0]["sf"] > g, "DAMAGE BEGIN after the damage (sf %d > %d)" % (begins[0]["sf"], g))
    check(core.warnings == [], "published prefix identical after every rerun (no warnings)")
    f1 = bs_f1()
    check(abs(core.f1_true_now() - f1[1]) < 1e-12 and abs(core.drop_true() - (f1[0] - f1[1]) / f1[0]) < 1e-12, "ground truth equals building_sim")
    st = core.state(2.0)
    check(st["truth"][-1][0] == g and st["damage"] == {"storey": 1, "loss": 0.3} and abs(st["f1_true_hz"] - f1[1]) < 1e-4, "twin/state truth history")
    core.apply({"cmd": "kill", "slave": 2})
    gk = core.last_cmd["sf"]
    pub_until(core, gk + 12)
    check(any(o["sf"] >= gk for o in find(core, "node", slave=2, state="DEAD")), "kill -> node DEAD line (firmware logic)")
    core.apply({"cmd": "revive", "slave": 2})
    gr = core.last_cmd["sf"]
    pub_until(core, gr + 10)
    check(any(o["sf"] >= gr for o in find(core, "node", slave=2, state="ALIVE")), "revive -> node ALIVE line")
    check(core.apply({"cmd": "revive", "slave": 2}).find("not dead") > 0, "revive of an alive sensor is a no-op")
    core.apply({"cmd": "repair"})
    gp = core.last_cmd["sf"]
    pub_until(core, gp + 150)
    ends = [d for d in find(core, "damage", state="END") if d["sf"] > gp]
    check(len(ends) >= 1, "repair -> DAMAGE END eventually (sf %s)" % (ends[0]["sf"] if ends else "none"))
    check(core.warnings == [], "no prefix warnings through kill/revive/repair")


def bs_f1():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "data", "generator"))
    import building_sim as bs
    p = bs.building_params(ts.DEFAULT_SEED)
    return bs.f1_hz(p), bs.f1_hz(p, (1, 0.3))


def test_storey10(wd):
    core = ts.TwinCore(workdir=wd)
    pub_until(core, 125)
    msg = core.apply({"cmd": "damage", "storey": 10, "loss": 0.3})
    check("no alarm is expected" in msg, "storey 10 log says no alarm expected")
    pub_until(core, 260)
    check(find(core, "damage", state="BEGIN") == [], "storey 10 by 30 %: no DAMAGE alarm (f1 barely moves)")


def test_parse_hostile():
    cases = [b'{"cmd":"damage","storey":NaN,"loss":0.3}', b'{"cmd":"damage","storey":Infinity,"loss":0.3}', b'{"cmd":"kill","slave":-Infinity}',
             b'{"cmd":"impact","floor":NaN}', b'{"cmd":"damage","storey":1,"loss":Infinity}', b'{"cmd":"damage","storey":1e400,"loss":0.3}',
             b'{"cmd":"damage","storey":"1","loss":0.3}', b'{"cmd":"damage","storey":{"a":1},"loss":0.3}', b'{"cmd":"damage","storey":[1],"loss":[0.3]}',
             b'{"cmd":"kill","slave":1.5}', b'[' * 200 + b']' * 200, b'{"cmd":"damage","storey":1,"loss":1e-400}',
             b'{"cmd":"damage","storey":' + b"9" * 200 + b',"loss":0.3}', b'{"cmd":"damage","storey":1,"loss":' + b"9" * 200 + b'}', b'\x00', b'',
             b'{"cmd":"kill","slave":true}', b'{"cmd":"damage","storey":2,"loss":-0.3}']
    out = []
    for c in cases:
        try:
            out.append(ts.parse_command(c))
        except Exception as e:                      # noqa: BLE001
            raise AssertionError("parse_command raised on %r: %r" % (c[:40], e))
    check(all(o is None for o in out), "%d hostile payloads (NaN, Infinity, 1e400, huge ints, nesting, wrong types) all rejected, none raises" % len(cases))
    ok = [ts.parse_command(b'{"cmd":"damage","storey":1.0,"loss":0.05}'), ts.parse_command(b'{"cmd":"damage","storey":10,"loss":0.5}'), ts.parse_command(b'{"cmd":"reset"}  ')]
    check(all(ok), "boundary values (storey 1.0, loss 0.05 / 0.5, trailing blanks) are accepted")


def test_spacing_and_noops(wd):
    core = ts.TwinCore(workdir=wd)
    pub_until(core, 110)
    core.apply({"cmd": "kill", "slave": 2})
    runs = core.runs
    g = core.last_cmd["sf"]
    m = core.apply({"cmd": "revive", "slave": 2})            # immediately: 0 superframes later
    check("too soon" in m and core.changed is False and core.runs == runs and 2 in core.killed, "a command inside MIN_SPACING_SF is refused (no netsim run): " + m)
    core.apply({"cmd": "kill", "slave": 2})
    check(core.changed is False and core.runs == runs, "no-op command (kill of a dead sensor) causes no netsim run and no state change")
    pub_until(core, g + ts.MIN_SPACING_SF)
    m = core.apply({"cmd": "revive", "slave": 2})
    check(core.changed is True and "revived" in m and core.last_cmd["sf"] - g >= ts.MIN_SPACING_SF, "after the spacing the command is applied (sf %d vs %d)" % (core.last_cmd["sf"], g))
    pub_until(core, core.last_cmd["sf"] + 8)
    check(any(o["slave"] == 2 and o["state"] == "DEAD" and g <= o["sf"] for o in find(core, "node")), "kill then revive with the spacing: the DEAD line is visible")
    for _ in range(40):
        core.cur_sf = 5
        core.apply({"cmd": "shake"})
    check(len(core.log) <= ts.MAX_LOG, "core log is capped (%d lines)" % len(core.log))


def test_state_scaling(wd):
    core = ts.TwinCore(workdir=wd)
    pub_until(core, 110)
    for i in range(ts.MAX_TRUTH + 50):
        core.cur_sf = 200 + i * 10
        core.last_g = -1
        core.commission_sf = 0
        core.apply({"cmd": "damage", "storey": 1 + i % 10, "loss": 0.1}, refresh=False)
    st = core.state(2.0)
    check(len(core.truth) <= ts.MAX_TRUTH and core.truth[0][0] == 0 and len(st["truth"]) <= ts.MAX_TRUTH, "truth history is capped (%d entries, first kept)" % len(st["truth"]))
    check(core.sf_to_t_us(50) == ts.line_field(next(l for l in core.lines if ts.line_field(l, "sf") == 50 and ts.line_field(l, "type") == "sample"), "t_us"), "sf -> t_us comes from an index")


class _Msg:
    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, payload, retain


class _Stub:
    class client:                                    # noqa: N801
        published = []

        @staticmethod
        def publish(t, payload, qos=1, retain=False):
            _Stub.client.published.append((t, payload, retain))

    def __init__(self):
        self.times = []
        self.lines = []

    def send_line(self, line):
        self.times.append(time.monotonic())
        self.lines.append(line)

    def finish(self, offline=True):
        pass


def test_flood(wd):
    """60 valid 'shake' commands at 20/s against a running server (speed 30, no broker): the stream must not stall, few are applied."""
    import argparse
    import threading
    import tempfile
    args = argparse.Namespace(site="twin-test", speed=30.0, seed=ts.DEFAULT_SEED, workdir=wd, netsim_exe=ts.DEFAULT_EXE, qos=1, host="x", port=1,
                              transport="tcp", ws_path="/mqtt", tls=False, user=None, password=None)
    srv = ts.TwinServer(args)
    srv.pub = _Stub()
    _Stub.client.published.clear()
    srv.pub.client = _Stub.client
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    t0 = time.time()
    while srv.core.cur_sf < 105 and time.time() - t0 < 60:
        time.sleep(0.1)
    check(srv.core.cur_sf >= 105, "server commissioned (sf %d)" % srv.core.cur_sf)
    time.sleep(2.2)
    sf0, n0 = srv.core.cur_sf, len(srv.pub.times)
    cmd = ts.rm.topic("twin-test", "twin/cmd")
    for i in range(60):
        srv.on_message(None, None, _Msg(cmd, b'{"cmd":"shake"}'))
        time.sleep(0.05)
    srv.on_message(None, None, _Msg(cmd, b'{"cmd":"damage","storey":NaN,"loss":0.3}'))   # must not kill the server
    time.sleep(10)
    srv.stop = True
    th.join(20)
    times = srv.pub.times[n0:]
    gaps = [b - a for a, b in zip(times, times[1:])]
    logs = [p.decode() for t, p, r in _Stub.client.published if t.endswith("twin/log")]
    applied = sum("building-wide shake" in l for l in logs)
    check(not th.is_alive(), "server thread survived the flood and the NaN command and stopped cleanly")
    check(max(gaps) < 6.0 and srv.core.cur_sf > sf0 + 60, "stream never stalled: max gap between published lines %.1f s, sf %d -> %d" % (max(gaps), sf0, srv.core.cur_sf))
    check(1 <= applied <= 4, "flood of 60 commands in 3 s: %d applied (rate limit at receive time, 1 pending)" % applied)
    check(srv.core.warnings == [], "no prefix warnings during the flood")
    states = [json.loads(p) for t, p, r in _Stub.client.published if t.endswith("twin/state")]
    check(all("updated_t" in s and s["period_s"] == ts.STATE_PERIOD_S for s in states), "every twin/state carries updated_t and period_s")


if __name__ == "__main__":
    import tempfile
    base = tempfile.mkdtemp(prefix="twintest_")
    test_commands()
    test_timeline(base)
    test_scenario(os.path.join(base, "a"))
    test_storey10(os.path.join(base, "b"))
    test_parse_hostile()
    test_spacing_and_noops(os.path.join(base, "c"))
    test_state_scaling(os.path.join(base, "d"))
    test_flood(os.path.join(base, "e"))
    print("%d checks passed" % N)
