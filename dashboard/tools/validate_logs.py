"""Validates JSON-lines event logs against the Resonance schema 2 (schema 1 logs are accepted as the subset).
Usage:  .venv/Scripts/python.exe dashboard/tools/validate_logs.py FILE...   Exit code 1 if any file has an error.
Checks: exact key sets and types per line type (meta, restart, sample, begin/change/status, node), enums, null rules, t_us
non-decreasing, sample/closed consistency, sample vs event agreement for the same superframe, restart epoch chain,
commission_n and age ranges, and for `node` lines: slave 1|2, state DEAD|ALIVE, DEAD/ALIVE alternate per slave (a master restart
or a new meta line forgets the state: the master starts with both slaves alive), and the next sample's `alive` bit for that slave
agrees with the node line. A second meta line (hardware reboot) restarts the closed/events counters.
"""
import json
import sys

CLASSES = {"COMMISSIONING", "AMBIENT", "LOCAL", "BUILDING", "NO_DATA"}
META_KEYS = {"type", "schema", "source", "rule", "T", "use_raw", "debounce", "commission_windows", "status_every_sf",
             "superframe_s", "floors", "epoch", "samples"}
META_KEYS_V1 = {"type", "schema", "rule", "T", "use_raw", "commission_windows", "status_every_sf", "superframe_s"}
EVENT_KEYS_V1 = {"type", "t_us", "sf", "class", "slave", "degraded_alive", "r1", "r2", "base1", "base2", "alive", "closed", "events"}
EVENT_KEYS_V2 = EVENT_KEYS_V1 | {"commission_n"}
SAMPLE_KEYS = {"type", "t_us", "sf", "class", "slave", "degraded_alive", "r1", "r2", "alive", "age", "commission_n", "closed"}
RESTART_KEYS = {"type", "t_us", "epoch", "prev_epoch"}
NODE_KEYS = {"type", "t_us", "sf", "slave", "state"}


def is_int(x, lo=0, hi=None):
    return isinstance(x, int) and not isinstance(x, bool) and x >= lo and (hi is None or x <= hi)


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")


def validate(path):
    errs = []

    def err(n, msg):
        if len(errs) < 40:
            errs.append(f"{path}:{n}: {msg}")

    try:
        raw = open(path, "rb").read()
    except OSError as e:
        return [f"{path}: {e}"], {}
    if b"\r" in raw:
        errs.append(f"{path}: contains CR characters (expected LF only)")
    text = raw.decode("utf-8")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    else:
        errs.append(f"{path}: last line is not terminated by a newline")
    stats = {"lines": len(lines), "meta": 0, "sample": 0, "begin": 0, "change": 0, "status": 0, "restart": 0, "node": 0}
    schema = None
    last_t = -1
    meta = None
    last_epoch = None
    meta_n = -10   # line number of the last meta line
    last_closed = 0
    last_events = 0
    first_event_closed = None
    samples_by_sf = {}   # (closed) -> sample
    n_events_changes = 0
    node_state = {1: True, 2: True}   # alive?
    node_pending = {}                 # slave -> alive expected in the next sample
    for n, line in enumerate(lines, 1):
        if not line:
            err(n, "empty line")
            continue
        try:
            o = json.loads(line)
        except ValueError as e:
            err(n, f"bad JSON: {e}")
            continue
        if not isinstance(o, dict):
            err(n, "not an object")
            continue
        ty = o.get("type")
        if n == 1:
            if ty != "meta":
                err(n, "first line must be meta")
        if ty == "meta":
            stats["meta"] += 1
            if stats["meta"] > 1:   # hardware reboot: counters start again
                last_t = -1
                last_closed = last_events = 0
                samples_by_sf = {}
                node_state = {1: True, 2: True}
                node_pending = {}
            schema = o.get("schema")
            if schema not in (1, 2):
                err(n, f"schema {schema}")
            keys = META_KEYS if schema == 2 else META_KEYS_V1
            if set(o) != keys:
                err(n, f"meta keys {sorted(set(o) ^ keys)} differ")
            if schema == 2:
                if not isinstance(o.get("source"), str):
                    err(n, "source")
                f = o.get("floors")
                if not (isinstance(f, list) and len(f) == 2 and all(is_int(x, -100, 1000) for x in f)):
                    err(n, f"floors {f}")
                if not (o.get("epoch") is None or is_int(o.get("epoch"), 1, 2**32 - 1)):
                    err(n, "epoch")
                if not isinstance(o.get("samples"), bool):
                    err(n, "samples")
                if not is_int(o.get("debounce"), 1, 3):
                    err(n, "debounce")
                last_epoch = o.get("epoch")
            if not isinstance(o.get("rule"), str) or o["rule"] not in "abcd" or len(o["rule"]) != 1:
                err(n, "rule")
            if not is_num(o.get("T")):
                err(n, "T")
            if not isinstance(o.get("use_raw"), bool):
                err(n, "use_raw")
            if not is_int(o.get("commission_windows"), 1, 20):
                err(n, "commission_windows")
            if not is_int(o.get("status_every_sf"), 1):
                err(n, "status_every_sf")
            if not is_num(o.get("superframe_s")) or o["superframe_s"] <= 0:
                err(n, "superframe_s")
            meta = o
            meta_n = n
            continue
        if ty == "restart":
            stats["restart"] += 1
            if set(o) != RESTART_KEYS:
                err(n, f"restart keys {sorted(set(o) ^ RESTART_KEYS)}")
            if not is_int(o.get("t_us")):
                err(n, "restart t_us")
            elif o["t_us"] < last_t:
                err(n, "restart t_us goes backwards")
            else:
                last_t = o["t_us"]
            if not is_int(o.get("epoch"), 1, 2**32 - 1):
                err(n, "restart epoch")
            if not (o.get("prev_epoch") is None or is_int(o.get("prev_epoch"), 1, 2**32 - 1)):
                err(n, "restart prev_epoch")
            if o.get("prev_epoch") is not None and last_epoch is not None and o["prev_epoch"] != last_epoch:
                err(n, f"prev_epoch {o['prev_epoch']} != previous epoch {last_epoch}")
            # The firmware's boot pattern is allowed: right after its meta line it prints restart with the SAME epoch and
            # prev_epoch null (the host cannot tell a first boot from a reboot). Anywhere else an unchanged epoch is an error.
            boot_pattern = (n == meta_n + 1 and o.get("prev_epoch") is None)
            if o.get("epoch") == last_epoch and not boot_pattern:
                err(n, "restart with unchanged epoch")
            last_epoch = o.get("epoch")
            node_state = {1: True, 2: True}
            node_pending = {}
            continue
        if ty == "node":
            stats["node"] += 1
            if schema != 2:
                err(n, "node line in a schema 1 log")
            if set(o) != NODE_KEYS:
                err(n, f"node keys {sorted(set(o) ^ NODE_KEYS)}")
                continue
            if not is_int(o["t_us"]):
                err(n, "node t_us")
            elif o["t_us"] < last_t:
                err(n, f"node t_us goes backwards ({o['t_us']} < {last_t})")
            else:
                last_t = o["t_us"]
            if not is_int(o["sf"]):
                err(n, "node sf")
            if o["slave"] not in (1, 2) or isinstance(o["slave"], bool):
                err(n, f"node slave {o['slave']}")
                continue
            if o["state"] not in ("DEAD", "ALIVE"):
                err(n, f"node state {o['state']}")
                continue
            alive = o["state"] == "ALIVE"
            if node_state[o["slave"]] == alive:
                err(n, f"node slave {o['slave']} goes {o['state']} but was already {o['state']}")
            node_state[o["slave"]] = alive
            node_pending[o["slave"]] = alive
            continue
        if ty not in ("sample", "begin", "change", "status"):
            err(n, f"unknown type {ty}")
            continue
        stats[ty] += 1
        if ty == "sample":
            if schema != 2:
                err(n, "sample line in a schema 1 log")
            keys = SAMPLE_KEYS
        else:
            keys = EVENT_KEYS_V2 if schema == 2 else EVENT_KEYS_V1
        if set(o) != keys:
            err(n, f"{ty} keys {sorted(set(o) ^ keys)} differ")
            continue
        if not is_int(o["t_us"]):
            err(n, "t_us")
        elif o["t_us"] < last_t:
            err(n, f"t_us goes backwards ({o['t_us']} < {last_t})")
        else:
            last_t = o["t_us"]
        if not is_int(o["sf"]):
            err(n, "sf")
        if o["class"] not in CLASSES:
            err(n, "class")
        if o["slave"] not in (0, 1, 2):
            err(n, "slave")
        if (o["class"] == "LOCAL") != (o["slave"] != 0):
            err(n, "slave must be non-zero exactly for LOCAL")
        if o["degraded_alive"] not in (0, 1, 2):
            err(n, "degraded_alive")
        if o["class"] in ("BUILDING", "COMMISSIONING", "NO_DATA") and o["degraded_alive"]:
            err(n, f"{o['class']} cannot be degraded")
        for k in ("r1", "r2"):
            if not (o[k] is None or (is_num(o[k]) and o[k] >= 0)):
                err(n, k)
        if o["class"] == "COMMISSIONING" and (o["r1"] is not None or o["r2"] is not None):
            err(n, "ratios during commissioning must be null")
        if not (isinstance(o["alive"], list) and len(o["alive"]) == 2 and all(isinstance(x, bool) for x in o["alive"])):
            err(n, "alive")
        if not is_int(o["closed"], 1):
            err(n, "closed")
        cn = o.get("commission_n")
        if schema == 2:
            if not (isinstance(cn, list) and len(cn) == 2 and all(is_int(x, 0, 255) for x in cn)):
                err(n, "commission_n")
            elif meta and any(x > meta["commission_windows"] for x in cn):
                err(n, "commission_n above commission_windows")
        if ty == "sample":
            age = o["age"]
            if not (isinstance(age, list) and len(age) == 2 and all(a is None or is_int(a) for a in age)):
                err(n, "age")
            else:
                for i in (0, 1):
                    if o["r1" if i == 0 else "r2"] is not None and age[i] != 0:
                        err(n, f"r{i+1} present but age != 0")
            if isinstance(o["alive"], list) and len(o["alive"]) == 2:
                for sl, want in node_pending.items():
                    if o["alive"][sl - 1] != want:
                        err(n, f"sample alive[{sl - 1}]={o['alive'][sl - 1]} disagrees with the preceding node line ({'ALIVE' if want else 'DEAD'})")
                node_pending = {}
            if is_int(o["closed"]):
                if o["closed"] != last_closed + 1:
                    err(n, f"sample closed {o['closed']} after {last_closed}")
                last_closed = o["closed"]
                samples_by_sf[o["closed"]] = o
        else:
            if ty in ("begin", "change"):
                if not is_int(o["events"]) or o["events"] != last_events + 1:
                    err(n, f"events counter {o['events']} after {last_events}")
                else:
                    last_events = o["events"]
            else:
                if o["events"] != last_events:
                    err(n, f"status events {o['events']} != {last_events}")
            for k in ("base1", "base2"):
                if not (is_num(o[k]) and o[k] >= 0):
                    err(n, k)
            if ty == "begin" and o["class"] not in ("LOCAL", "BUILDING"):
                err(n, "begin must be LOCAL or BUILDING")
            if schema == 2 and meta and meta.get("samples") and is_int(o["closed"]):
                s = samples_by_sf.get(o["closed"])
                if s is None:
                    err(n, "event without a sample for the same closed superframe")
                else:
                    for k in ("t_us", "sf", "class", "slave", "degraded_alive", "r1", "r2", "alive", "commission_n"):
                        if s[k] != o[k]:
                            err(n, f"event {k}={o[k]} differs from the sample of the same superframe ({s[k]})")
    if meta and schema == 2 and meta.get("samples") and stats["sample"] == 0:
        errs.append(f"{path}: meta says samples=true but there are none")
    if meta and schema == 2 and not meta.get("samples") and stats["sample"]:
        errs.append(f"{path}: meta says samples=false but there are {stats['sample']}")
    if stats["meta"] != 1 and not (stats["meta"] > 1):
        errs.append(f"{path}: expected a meta line")
    return errs, stats


def main():
    bad = 0
    for p in sys.argv[1:]:
        errs, stats = validate(p)
        status = "OK  " if not errs else "FAIL"
        print(f"{status} {p}  {stats}")
        for e in errs:
            print("   ", e)
        bad += bool(errs)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
