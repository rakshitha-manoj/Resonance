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


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok  ", name)
