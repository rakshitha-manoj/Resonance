"""Tests of the restart/epoch rule of validate_logs.py: python dashboard/tools/test_validate_logs.py"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import validate_logs as v  # noqa: E402

DEMO = os.path.join(os.path.dirname(HERE), "demo_events_sim.jsonl")
LINES = [ln.rstrip("\r\n") for ln in open(DEMO, encoding="utf-8")]
META = LINES[0]
E = json.loads(META)["epoch"]


def check(lines):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.jsonl")
        with open(p, "w", newline="\n", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return v.validate(p)[0]


def restart(t, epoch, prev):
    return json.dumps({"type": "restart", "t_us": t, "epoch": epoch, "prev_epoch": prev}, separators=(",", ":"))


def test_unmodified_demo_is_ok():
    assert check(LINES) == []


def test_firmware_boot_pattern_is_accepted():
    # meta (epoch E), restart (epoch E, prev null) directly after it, then the data lines
    assert check([META, restart(0, E, None)] + LINES[1:]) == []


def test_unchanged_epoch_elsewhere_is_still_an_error():
    errs = check(LINES[:3] + [restart(json.loads(LINES[2])["t_us"], E, None)] + LINES[3:])
    assert any("unchanged epoch" in e for e in errs), errs
    errs = check([META, restart(0, E, E)] + LINES[1:])   # right after meta but prev_epoch is not null: not the boot pattern
    assert any("unchanged epoch" in e for e in errs), errs


def with_damage(lines, k, begin_drop=0.0205, end_at=None, end_drop=0.004):
    """Schema-3 demo with a DAMAGE episode: the k-th sample line (0-based among samples) flips dmg to 1 (BEGIN line right after it);
    from there on every sample / event carries dmg 1, until the sample end_at (if given) flips it back (END line)."""
    out, ns, state = [], -1, 0
    for ln in lines:
        o = json.loads(ln)
        if o["type"] in ("sample", "begin", "change", "status"):
            if o["type"] == "sample":
                ns += 1
                if ns == k:
                    state = 1
                if end_at is not None and ns == end_at:
                    state = 0
            o["dmg"] = state
        out.append(json.dumps(o, separators=(",", ":")))
        if o["type"] == "sample" and ns == k and state == 1 and o["dmg"] == 1 and (ns == k):
            out.append(json.dumps({"type": "damage", "t_us": o["t_us"], "sf": o["sf"], "state": "BEGIN", "drop": begin_drop,
                                   "f1": [5.2, None], "f1_0": [5.4, 6.0]}, separators=(",", ":")))
        if o["type"] == "sample" and end_at is not None and ns == end_at:
            out.append(json.dumps({"type": "damage", "t_us": o["t_us"], "sf": o["sf"], "state": "END", "drop": end_drop,
                                   "f1": [5.4, 6.0], "f1_0": [5.4, 6.0]}, separators=(",", ":")))
    return out


def test_schema3_demo_and_legacy_schema2_both_valid():
    assert json.loads(LINES[0])["schema"] == 3
    legacy = os.path.join(os.path.dirname(HERE), "sample_events.jsonl")   # the scripted page demo, still schema 2
    assert json.loads(open(legacy, encoding="utf-8").readline())["schema"] == 2
    assert v.validate(legacy)[0] == []


def test_damage_episode_valid_and_every_break_is_caught():
    good = with_damage(LINES, 60, end_at=90)
    assert check(good) == []
    assert v.validate  # noqa
    # BEGIN without the flipping sample / END without BEGIN
    no_line = [ln for ln in good if '"type":"damage"' not in ln]
    assert any("no damage line follows" in e for e in check(no_line)), check(no_line)
    only_begin = [ln for ln in good if '"state":"END"' not in ln]
    assert any("flipped dmg" in e or "no damage line" in e for e in check(only_begin)), check(only_begin)
    # drop below delta at BEGIN, above delta/2 at END
    assert any("BEGIN with pooled drop" in e for e in check(with_damage(LINES, 60, end_at=90, begin_drop=0.005)))
    assert any("END with pooled drop" in e for e in check(with_damage(LINES, 60, end_at=90, end_drop=0.015)))
    # wrong superframe / duplicate BEGIN
    bad_sf = [ln.replace('"state":"BEGIN"', '"state":"BEGIN","sf_x":1') for ln in good]
    assert any("damage keys" in e for e in check(bad_sf))
    dup = []
    for ln in good:
        dup.append(ln)
        if '"state":"BEGIN"' in ln:
            dup.append(ln)
    assert check(dup) != []
    # a damage line in a schema 2 log
    s2 = [ln.replace('"schema":3', '"schema":2') if '"type":"meta"' in ln else ln for ln in good]
    assert check(s2) != []


def test_schema3_field_rules():
    lines = list(LINES)
    i = next(k for k, ln in enumerate(lines) if '"type":"sample"' in ln and '"sf":50,' in ln)
    o = json.loads(lines[i])
    for key, val, what in (("ae", [-1.0, None], "ae"), ("f1", [150.0, None], "f1"), ("drop", [2.0, None], "drop"), ("dmg", 2, "dmg"),
                           ("ae", [1.0], "ae")):
        b = dict(o)
        b[key] = val
        errs = check(lines[:i] + [json.dumps(b, separators=(",", ":"))] + lines[i + 1:])
        assert any(what in e for e in errs), (key, errs)
    b = dict(o)
    b["drop"] = [0.01, None]
    b["f1"] = [None, None]
    assert any("drop without f1" in e for e in check(lines[:i] + [json.dumps(b, separators=(",", ":"))] + lines[i + 1:]))
    # a slave that did not report carries no values
    b = dict(o)
    b["age"] = [3, 0]
    b["r1"] = None
    b["ae"] = [1.0, None]
    assert any("did not report" in e for e in check(lines[:i] + [json.dumps(b, separators=(",", ":"))] + lines[i + 1:]))
    # sample / event of the same superframe must agree on the new fields
    j = next(k for k, ln in enumerate(lines) if '"type":"begin"' in ln)
    e = json.loads(lines[j])
    e["ae"] = [99.0, 99.0]
    assert any("differs from the sample" in x for x in check(lines[:j] + [json.dumps(e, separators=(",", ":"))] + lines[j + 1:]))
    # meta: rule f needs T_b and gate, dmg constants
    m = json.loads(META)
    assert m["rule"] == "f" and m["T"] == 6 and m["T_b"] == 3 and m["dmg_p"] == 32
    del m["T_b"]
    assert any("meta keys" in x for x in check([json.dumps(m, separators=(",", ":"))] + LINES[1:]))


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok  ", name)
