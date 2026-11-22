"""Offline tests of tools/twin_server.py (no MQTT; needs the netsim build: `pio run -e netsim` in firmware/esp32).
Run: .venv\\Scripts\\python.exe tools\\test_twin.py"""
import json
import os
import sys

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


if __name__ == "__main__":
    import tempfile
    base = tempfile.mkdtemp(prefix="twintest_")
    test_commands()
    test_timeline(base)
    test_scenario(os.path.join(base, "a"))
    test_storey10(os.path.join(base, "b"))
    print("%d checks passed" % N)
