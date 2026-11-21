#!/usr/bin/env python
"""End-to-end test of the MQTT path on a real broker (default: the free public HiveMQ, simulator data only).

  1. runs netsim on the demo traces -> the reference JSONL (what the dashboard / validator already know),
  2. subscribes (paho) to resonance/<site>/# BEFORE publishing,
  3. runs tools/sim_publisher.py as a separate process at --speed (default 0 = as fast as possible),
  4. checks that EVERY line arrives, byte-identical and in publish order, on the topic its type maps to, that the retained
     .../state equals the last decision line, that meta is retained, and that status goes online -> offline.
Retained copies that the broker hands out on subscribe (a previous run on the same site) are recognised by the retain flag
and kept apart. Exit code 0 = all checks passed.
    python tools/e2e_mqtt_test.py [--site rsn-822298ab] [--speed 0] [--jsonl FILE]
"""
import argparse
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resonance_mqtt as rm  # noqa: E402
import sim_publisher as sp  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    rm.add_connection_args(ap)
    ap.add_argument("--speed", default="0")
    ap.add_argument("--jsonl", help="reference/published log (default: run netsim)")
    ap.add_argument("--netsim-args", default="")
    ap.add_argument("--timeout", type=float, default=90)
    args = ap.parse_args()

    if args.jsonl:
        expected = sp.read_lines(args.jsonl)
    else:
        expected = sp.netsim_lines(os.path.join(sp.FW, ".pio", "build", "netsim", "program.exe"), os.path.join("test", "data", "demo_traces.bin"),
                                   args.netsim_args.split() if args.netsim_args else [])
    print("reference: %d lines" % len(expected))
    site = args.site
    prefix = "resonance/%s/" % site

    got = []          # (leaf, payload, retain) live messages on the main topics, in arrival order
    state_msgs = []   # live .../state messages
    status_msgs = []  # live .../status messages
    retained_old = []
    lock = threading.Lock()
    done = threading.Event()
    last_activity = [time.time()]

    c = rm.make_client(args, client_id="e2e-sub-%d" % os.getpid())

    def on_connect(cl, ud, flags, rc, props=None):
        cl.subscribe(prefix + "#", qos=1)

    def on_message(cl, ud, msg):
        leaf = msg.topic[len(prefix):]
        payload = msg.payload
        with lock:
            last_activity[0] = time.time()
            if msg.retain:   # stale retained copies from an earlier run on this site (a live message is delivered with retain=0)
                retained_old.append((leaf, payload, True))
            elif leaf == "status":
                status_msgs.append((payload, False))
            elif leaf == "state":
                state_msgs.append((leaf, payload, False))
            else:
                got.append((leaf, payload, False))
            if status_msgs and status_msgs[-1][0] == b"offline" and not status_msgs[-1][1]:
                done.set()

    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(args.host, args.port, keepalive=30)
    c.loop_start()
    time.sleep(6)  # subscription + retained replay from an earlier run (the public broker can take seconds)

    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_publisher.py"), "--speed", str(args.speed),
           "--site", site, "--host", args.host, "--port", str(args.port),
           "--transport", args.transport, "--ws-path", args.ws_path]
    if args.tls:
        cmd.append("--tls")
    if args.user:
        cmd += ["--user", args.user]   # the password reaches the child through the MQTT_PASSWORD environment variable
    env = dict(os.environ)
    if args.password:
        env["MQTT_PASSWORD"] = args.password
    if args.jsonl:
        cmd += ["--jsonl", args.jsonl]
    if args.netsim_args:
        cmd += ["--netsim-args", args.netsim_args]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    pub_s = time.time() - t0
    print("publisher exit %d in %.1f s: %s" % (r.returncode, pub_s, (r.stderr.strip().splitlines() or [""])[-1]))
    done.wait(args.timeout)
    # wait until the stream has been quiet for 3 s (live messages still in flight)
    while time.time() - last_activity[0] < 3 and time.time() - t0 < args.timeout:
        time.sleep(0.2)
    c.loop_stop()
    c.disconnect()

    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + msg)
        ok &= bool(cond)

    exp_topics = [rm.route(ln).leaf for ln in expected]
    live = [(leaf, p) for leaf, p, ret in got]
    check(r.returncode == 0, "publisher exit code 0")
    check(len(live) == len(expected), "received %d lines, expected %d" % (len(live), len(expected)))
    # MQTT orders messages per topic only (a broker cluster may interleave different topics), so the exact check is per topic:
    # every line byte-identical and in publish order within its topic. The global order is reported (the dashboard re-sorts by time).
    by_leaf_exp, by_leaf_got = {}, {}
    for ln, leaf in zip(expected, exp_topics):
        by_leaf_exp.setdefault(leaf, []).append(ln.encode("utf-8"))
    for leaf, p in live:
        by_leaf_got.setdefault(leaf, []).append(p)
    mism = sum(1 for leaf in set(by_leaf_exp) | set(by_leaf_got)
               for a, b in zip(by_leaf_got.get(leaf, []), by_leaf_exp.get(leaf, [])) if a != b)
    mism += sum(abs(len(by_leaf_got.get(leaf, [])) - len(by_leaf_exp.get(leaf, []))) for leaf in set(by_leaf_exp) | set(by_leaf_got))
    check(mism == 0, "every line byte-identical, in publish order within its topic, on the topic its type maps to (mismatches: %d)" % mism)
    glob = sum(1 for a, b in zip(live, expected) if a[1] != b.encode("utf-8"))
    print("INFO  global arrival order differs from publish order at %d of %d positions (cross-topic order is not an MQTT guarantee)" % (glob, len(expected)))
    import json
    rank = {"meta": -1, "restart": 0, "node": 1, "sample": 2}   # same tie-break as the dashboard's MQTT reorder buffer

    def skey(p):
        o = json.loads(p)
        return (o.get("t_us", -1), rank.get(o.get("type"), 3))

    resorted = [p for _, p in sorted(live, key=lambda lp: skey(lp[1]))]
    check(resorted == [e.encode("utf-8") for e in expected], "sorted by (t_us, line kind) the received stream equals the published stream line for line (exact global order restored)")
    check(sorted(p for _, p in live) == sorted(e.encode("utf-8") for e in expected), "the received lines are exactly the published lines (no loss, no duplicate, no change)")
    seen = {}
    for leaf, _ in live:
        seen[leaf] = seen.get(leaf, 0) + 1
    print("      per topic: " + ", ".join("%s=%d" % kv for kv in sorted(seen.items())))
    n_decision = sum(1 for ln in expected if rm.route(ln).to_state)
    check(len(state_msgs) == n_decision, "retained state updated for every decision line (%d of %d)" % (len(state_msgs), n_decision))
    last_decision = [ln for ln in expected if rm.route(ln).to_state][-1].encode("utf-8")
    check(state_msgs and state_msgs[-1][1] == last_decision, "last .../state message is the last decision line")
    check(any(s[0] == b"online" for s in status_msgs) and status_msgs and status_msgs[-1][0] == b"offline", "status online ... offline (%s)" % [s[0].decode() for s in status_msgs])

    # a fresh late subscriber must get the retained meta, state and status immediately
    late = {}
    c2 = rm.make_client(args, client_id="e2e-late-%d" % os.getpid())

    def on_msg2(cl, ud, msg):
        late[msg.topic[len(prefix):]] = (msg.payload, msg.retain)

    c2.on_connect = lambda cl, ud, f, rc, p=None: cl.subscribe(prefix + "#", qos=1)
    c2.on_message = on_msg2
    c2.connect(args.host, args.port, keepalive=30)
    c2.loop_start()
    t1 = time.time()
    while time.time() - t1 < 30 and not all(k in late for k in ("meta", "state", "status")):
        time.sleep(0.2)
    time.sleep(1)
    c2.loop_stop()
    c2.disconnect()
    meta_exp = [ln for ln in expected if rm.route(ln).kind == "meta"][-1].encode("utf-8")
    check(late.get("meta") == (meta_exp, True), "late subscriber receives the retained meta")
    check(late.get("state") == (last_decision, True), "late subscriber receives the retained state (latest decision)")
    check(late.get("status") == (b"offline", True), "late subscriber receives the retained status (offline)")
    print("SUMMARY lines sent %d, received %d, mismatches %d, publish time %.1f s -> %s" % (len(expected), len(live), mism, pub_s, "OK" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
