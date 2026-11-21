"""Plain-assert tests of the IoT PC tools (no broker, no network): python tools/test_iot_tools.py"""
import json
import os
import sys
import urllib.error

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
    assert T('{"type":"meta","schema":2}') == ("meta", "meta", True, False)
    assert T('{"type":"restart","t_us":5}').leaf == "restart" and not T('{"type":"restart"}').to_state
    s = T('{"type":"sample","t_us":1}')
    assert s.leaf == "sample" and s.to_state and not s.retain
    for ty in ("begin", "change"):
        assert T('{"type":"%s"}' % ty).to_state
    assert T('{"type":"status","t_us":1}').leaf == "status_line" and T('{"type":"status"}').to_state   # never the LWT topic
    assert T('{"type":"state"}').leaf == "state_line"
    d = T('{"type":"damage","ae":0.3}')
    assert d.leaf == "damage" and not d.to_state and d.kind == "damage"
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
def every_bundled_log_line_routes_to_a_known_leaf():
    for name in ("demo_events_sim.jsonl", "demo_events_faults_sim.jsonl"):
        for ln in open(os.path.join(REPO, "dashboard", name), encoding="utf-8"):
            r = rm.route(ln.rstrip("\r\n"))
            assert r.leaf in ("meta", "restart", "sample", "begin", "change", "status_line", "node"), (name, ln[:60])


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
    a = al.Alerter(None, al.RateLimiter(0, 1000), out=out.append)
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
    assert text.count("[dry run") == len(out)            # nothing sent without Telegram settings
    assert a.printed == len(out) > 5


@test
def retained_and_ended_events_do_not_alert():
    a = al.Alerter(None, al.RateLimiter(0, 100), out=lambda s: None)
    assert a.classify({"type": "begin", "class": "BUILDING", "sf": 1}, retained=True) is None
    assert a.classify({"type": "change", "class": "AMBIENT", "slave": 0}) is None       # event ended
    assert a.classify({"type": "sample", "class": "BUILDING"}) is None
    assert a.status("offline", retained=True) is None
    assert a.status("offline", retained=False)[1].startswith("The master went OFFLINE")
    assert a.classify({"type": "damage", "ae": 1.5, "f1": 7.1})[1].startswith("DAMAGE indication")
    assert a.classify({"type": "whatever"}) is None


@test
def telegram_token_never_printed():
    token = "123456:SECRET-TOKEN-abc"
    calls = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def ok_opener(req, timeout=0):
        calls.append((req.full_url, json.loads(req.data)))
        return Resp()

    out = []
    a = al.Alerter(al.Telegram(token, "42", opener=ok_opener), al.RateLimiter(0, 10), out=out.append, secrets=[token])
    assert a.emit("k", "BUILDING-WIDE vibration event", "rsn-x")
    assert calls[0][0] == al.API % token and calls[0][1]["chat_id"] == "42" and "rsn-x" in calls[0][1]["text"]
    assert all(token not in line for line in out), out

    def failing(req, timeout=0):   # an error whose text contains the URL (and so the token)
        raise urllib.error.URLError("failed to reach " + req.full_url)

    out2 = []
    a2 = al.Alerter(al.Telegram(token, "42", opener=failing), al.RateLimiter(0, 10), out=out2.append, secrets=[token])
    assert not a2.emit("k", "x", "rsn-x")
    assert a2.failed == 1 and out2 and all(token not in line for line in out2), out2
    assert al.scrub("a %s b" % token, [token, None]) == "a *** b"

    def http_err(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    s = al.Telegram(token, "42", opener=http_err).send("x")
    assert s == (False, "HTTP 401")


@test
def telegram_without_env_prints(monkeypatch=None):
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    r = subprocess.run([sys.executable, os.path.join(HERE, "alerter.py"), "--selftest-message", "--site", "rsn-test0001"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "[dry run" in r.stdout and "rsn-test0001" in r.stdout, (r.stdout, r.stderr)


if __name__ == "__main__":
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print("%d tests passed" % len(tests))
