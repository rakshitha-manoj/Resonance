"""Plain-assert tests of the IoT PC tools (no broker, no network): python tools/test_iot_tools.py"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import alerter as al  # noqa: E402
import resonance_mqtt as rm  # noqa: E402
import sim_publisher as sp  # noqa: E402

REPO = os.path.dirname(HERE)
tests = []


def test(f):
    tests.append(f)
    return f


@test
def topic_mapping_matches_firmware_vectors():
    # the same vectors as firmware/esp32/test/test_mqtt_logic (mqtt_logic.h)
    T = rm.route
    assert T('{"type":"meta","schema":2}') == ("meta", "meta", True, False, False)
    assert T('{"type":"restart","t_us":5}').leaf == "restart" and not T('{"type":"restart"}').to_state
    s = T('{"type":"sample","t_us":1}')
    assert s.leaf == "sample" and s.to_state and not s.retain
    for ty in ("begin", "change"):
        assert T('{"type":"%s"}' % ty).to_state
    assert T('{"type":"status","t_us":1}').leaf == "status_line" and T('{"type":"status"}').to_state   # never the LWT topic
    assert T('{"type":"state"}').leaf == "state_line"
    d = T('{"type":"damage","ae":0.3}')
    assert d.leaf == "damage" and not d.to_state and d.kind == "damage" and d.to_damage_state and not d.retain
    assert T('{"type":"damage_state"}').leaf == "damage_state_line"
    for bad in ("R,1,2,3", '{"x":1}', '{"type":""}', '{"type":"a/b"}', '{"type":"a+b"}', '{"type":"#"}', '{"type":"unterminated', "",
                '{"type":"%s"}' % ("a" * 25)):
        assert T(bad).leaf == "other", bad
    assert T('{"type":"%s"}' % ("a" * 24)).leaf == "a" * 24
    assert rm.topic("rsn-0123abcd", "sample") == "resonance/rsn-0123abcd/sample"
    assert rm.site_ok("rsn-822298ab") and not rm.site_ok("a/b") and not rm.site_ok("A") and not rm.site_ok("") and not rm.site_ok("a" * 33)
    for bad in ("bad/site", "UP", "a+", ""):
        try:
            rm.topic(bad, "x")
            raise AssertionError("accepted " + bad)
        except ValueError:
            pass


@test
def status_payload_epoch_and_stale_offline():
    assert rm.status_payload(True, 2866824256) == b'{"state":"online","epoch":2866824256}'   # byte-identical to mqtt_logic.h
    assert rm.status_payload(False) == b'{"state":"offline","epoch":null}'
    assert rm.parse_status(b'{"state":"offline","epoch":5}') == ("offline", 5)
    assert rm.parse_status(b"online") == ("online", None)           # legacy text
    assert rm.parse_status(b"garbage") == (None, None)
    assert rm.offline_is_stale("offline", 4, 5)                      # the will of an older boot
    assert not rm.offline_is_stale("offline", 5, 5) and not rm.offline_is_stale("offline", None, 5)
    assert not rm.offline_is_stale("offline", 4, None) and not rm.offline_is_stale("online", 4, 5)
    a = al.Alerter(al.RateLimiter(0, 100), out=lambda x: None)
    a.classify({"type": "meta", "epoch": 7, "floors": [1, 2]})
    assert a.status(rm.status_payload(False, 6), False) is None      # stale will from epoch 6 after the boot with epoch 7
    assert a.status(rm.status_payload(False, 7), False)[1].startswith("The master went OFFLINE")
    assert a.status(b"offline", False) is not None                   # legacy text without epoch is accepted
    assert a.status(rm.status_payload(False, 7), True) is None       # retained copies never alert


@test
def alerter_forgets_previous_event_on_new_meta_or_restart():
    a = al.Alerter(al.RateLimiter(0, 100), out=lambda x: None)
    ev = {"type": "begin", "class": "BUILDING", "slave": 0, "sf": 3}
    assert a.classify(ev) is not None
    assert a.classify(dict(ev, type="change")) is None               # same key again: nothing new
    a.classify({"type": "restart", "epoch": 9})
    assert a.classify(dict(ev, type="change")) is not None           # after a restart the same event is news again
    a.classify({"type": "meta", "epoch": 10})
    assert a.prev_key is None


@test
def every_bundled_log_line_routes_to_a_known_leaf():
    for name in ("demo_events_sim.jsonl", "demo_events_faults_sim.jsonl"):
        for ln in open(os.path.join(REPO, "dashboard", name), encoding="utf-8"):
            r = rm.route(ln.rstrip("\r\n"))
            assert r.leaf in ("meta", "restart", "sample", "begin", "change", "status_line", "node", "damage"), (name, ln[:60])


@test
def pacer_real_time_speed_and_restart():
    now = [100.0]
    p = sp.Pacer(10.0, clock=lambda: now[0])
    assert p.due(None) == 0.0
    assert p.due(2_000_000) == 0.0                      # first timestamped line anchors the timeline
    assert abs(p.due(3_400_000) - 0.14) < 1e-9          # 1.4 s of sim time at 10x
    now[0] += 0.14
    assert abs(p.due(4_800_000) - 0.14) < 1e-9
    assert abs(p.due(2_800_000) - 0.28) < 1e-9          # t_us went backwards: the timeline continues one superframe after the last line (4.8 s + 1.4 s = 6.2 s sim)
    assert sp.Pacer(0).due(5_000_000) == 0.0             # speed 0 = no pacing
    now[0] = 1000.0
    assert p.due(5_000_000) == 0.0                       # already due -> no wait, never negative


@test
def looped_recording_is_schema_valid():
    import tempfile
    sys.path.insert(0, os.path.join(REPO, "dashboard", "tools"))
    import validate_logs as v
    for name in ("demo_events_sim.jsonl", "demo_events_faults_sim.jsonl"):
        lap1 = [ln.rstrip("\r\n") for ln in open(os.path.join(REPO, "dashboard", name), encoding="utf-8")]
        lap2 = list(sp.lap_lines(lap1))
        lap3 = list(sp.lap_lines(lap1))
        assert lap2[0] != lap1[0] and lap2[1].startswith('{"type":"restart","t_us":0,"epoch":')
        keep = lambda L: [x for x in L if rm.route(x).kind not in ('meta', 'restart')]
        assert keep(lap2) == keep(lap1)                      # everything but the epochs / restart lines is byte for byte
        e2, e3 = json.loads(lap2[0])["epoch"], json.loads(lap3[0])["epoch"]
        assert json.loads(lap2[1])["epoch"] == e2 and json.loads(lap2[1])["prev_epoch"] is None
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "loop.jsonl")
            with open(f, "w", newline="\n", encoding="utf-8") as fh:
                fh.write("\n".join(lap1 + lap2 + lap3) + "\n")
            errs = v.validate(f)[0]
            assert errs == [], (name, errs[:3])


@test
def line_t_us():
    assert sp.line_t_us('{"type":"sample","t_us":123,"sf":1}') == 123
    assert sp.line_t_us('{"type":"meta","schema":2}') is None
    assert sp.line_t_us("garbage") is None


@test
def rate_limiter_cooldown_and_hourly_budget():
    t = [0.0]
    r = al.RateLimiter(cooldown_s=60, max_per_hour=3, clock=lambda: t[0])
    assert r.allow("a") == (True, 0)
    assert r.allow("a") == (False, 0)                    # cooldown
    assert r.allow("b") == (True, 1)                     # carries the count of the suppressed one
    t[0] = 61
    assert r.allow("a") == (True, 0)
    assert r.allow("c") == (False, 0)                    # budget of 3 per hour used up
    t[0] = 3700
    assert r.allow("c")[0]                               # window slid


@test
def alerts_from_the_fault_demo_log():
    out = []
    a = al.Alerter(al.RateLimiter(0, 1000), out=out.append)
    for ln in open(os.path.join(REPO, "dashboard", "demo_events_faults_sim.jsonl"), encoding="utf-8"):
        r = a.classify(json.loads(ln))
        if r:
            a.emit(r[0], r[1], "rsn-x")
    text = "\n".join(out)
    assert "LOCAL vibration event at floor 5 (sensor 1)" in text, text
    assert "BUILDING-WIDE vibration event" in text
    assert "LOCAL vibration event at floor 10 (sensor 2)" in text
    assert "Floor 10 (sensor 2) is OFFLINE" in text and "back ONLINE" in text, text
    assert "master restarted" in text
    assert text.count("ALERT  Resonance rsn-x:") == len(out)   # every alert is a printed line, nothing is sent anywhere
    assert a.printed == len(out) > 5


@test
def retained_and_ended_events_do_not_alert():
    a = al.Alerter(al.RateLimiter(0, 100), out=lambda s: None)
    assert a.classify({"type": "begin", "class": "BUILDING", "sf": 1}, retained=True) is None
    assert a.classify({"type": "change", "class": "AMBIENT", "slave": 0}) is None       # event ended
    assert a.classify({"type": "sample", "class": "BUILDING"}) is None
    assert a.status("offline", retained=True) is None
    assert a.status("offline", retained=False)[1].startswith("The master went OFFLINE")
    assert a.classify({"type": "damage", "ae": 1.5, "f1": 7.1})[1].startswith("DAMAGE indication")
    k, txt = a.classify({"type": "damage", "t_us": 1, "sf": 93, "state": "BEGIN", "drop": 0.0205, "f1": [5.25, None], "f1_0": [5.4, 6.0]})
    assert k == "damage:BEGIN" and txt.startswith("DAMAGE ALARM: natural frequency down 2.1 %") and "5.25/?" in txt and "5.40/6.00" in txt and len(txt) < 140, txt
    assert a.classify({"type": "whatever"}) is None


@test
def alert_texts_for_damage_and_ae_driven_events():
    a = al.Alerter(al.RateLimiter(0, 100), out=lambda s: None)
    a.classify({"type": "meta", "T": 3, "T_ae": 6, "floors": [5, 10], "epoch": 1})
    # frequency shift: only the AE is over its threshold
    k, txt = a.classify({"type": "begin", "t_us": 239330094, "sf": 170, "class": "LOCAL", "slave": 2, "r1": 1.02, "r2": 1.97, "ae": [0.9, 9.51]})
    assert txt == "LOCAL vibration event at floor 10 (sensor 2) [sensor 2: AE 9.5 >= 6] [sf 170, t=239.3 s]", txt
    a.classify({"type": "change", "class": "AMBIENT", "slave": 0})
    k, txt = a.classify({"type": "begin", "t_us": 1000000, "sf": 5, "class": "BUILDING", "r1": 4.2, "r2": 4.1, "ae": [2.0, 7.0]})
    assert "[sensor 1: std 4.2 >= 3; sensor 2: std 4.1 >= 3, AE 7.0 >= 6]" in txt and txt.startswith("BUILDING-WIDE"), txt
    k, txt = a.classify({"type": "damage", "sf": 300, "state": "END", "drop": 0.004, "f1": [7.0, 7.01], "f1_0": [7.03, 7.05]})
    assert k == "damage:END" and txt.startswith("DAMAGE cleared") and "0.4 %" in txt, txt
    k, txt = a.classify({"type": "damage", "sf": 310, "state": "BASELINE", "drop": None, "f1": [7.0, None], "f1_0": [7.0, None], "slave": 1})
    assert k == "damage:BASELINE:1" and txt.startswith("Note: floor 5 (sensor 1) re-commissioned") and "7.00" in txt, txt
    # no schema-3 fields (rule b log): no detector tag, same text as before
    b = al.Alerter(al.RateLimiter(0, 100), out=lambda s: None)
    assert b.classify({"type": "begin", "t_us": 0, "sf": 1, "class": "LOCAL", "slave": 1, "r1": 5.0, "r2": 1.0})[1].startswith("LOCAL vibration event at sensor 1 [sf 1")


@test
def alerter_selftest_prints_only():
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(HERE, "alerter.py"), "--selftest-message", "--site", "rsn-test0001"], capture_output=True, text=True)
    assert r.returncode == 0 and "ALERT  Resonance rsn-test0001: test message" in r.stdout, (r.stdout, r.stderr)


@test
def alerter_has_no_messaging_service_code():
    # The alerter only prints. It must not contain code that sends alerts to an external messaging service
    # or reads credentials from the environment.
    src = open(os.path.join(HERE, "alerter.py"), encoding="utf-8").read().lower()
    for word in ("urllib", "requests", "os.environ", "getenv", "api.", "token"):
        assert word not in src, word


if __name__ == "__main__":
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print("%d tests passed" % len(tests))
