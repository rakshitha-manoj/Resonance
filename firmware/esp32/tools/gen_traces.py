"""Writes the netsim demo window traces: firmware/esp32/test/data/demo_traces.bin

One simulated building (building_sim), two slaves on floors 5 and 10, scripted as consecutive recordings (1 window = 1 superframe):
  commissioning (healthy, level 1.0) | ambient | impact on slave 1's floor | ambient | 3x building-wide | ambient |
  frequency shift on slave 2's floor | ambient.
Each segment is a separate recording (new excitation realisation, same building and sensors).

Regenerate:   .venv/Scripts/python.exe firmware/esp32/tools/gen_traces.py [--seed N] [--out FILE]
Run:          cd firmware/esp32 && pio run -e netsim &&
              .pio/build/netsim/program.exe --traces test/data/demo_traces.bin --fusion --events-jsonl demo_events.jsonl
File layout (little endian): "RTRC" | u32 version=1 | u32 n_slaves=2 | u32 n_windows | slave 1 then slave 2:
n_windows x (128 int16 az, 128 int16 gx). See netsim::TraceSource in include/netsim.h.
"""
import argparse
import struct
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "data" / "generator"))
sys.dont_write_bytecode = True
import building_sim as bs  # noqa: E402

DEFAULT_OUT = ROOT / "firmware" / "esp32" / "test" / "data" / "demo_traces.bin"
DEFAULT_SEED = 5_990_001
FLOORS = (5, 10)
# (name, windows, excitation id, amplitude multiplier, impact floor, shift floor)
SCRIPT = [
    ("commissioning", 24, 0, 1.0, None, None),
    ("ambient", 20, 1, 1.0, None, None),
    ("impact on slave 1 (floor 5)", 12, 2, 1.0, 5, None),
    ("ambient", 12, 3, 1.0, None, None),
    ("3x building-wide", 12, 4, 3.0, None, None),
    ("ambient", 12, 5, 1.0, None, None),
    ("frequency shift on slave 2 (floor 10)", 12, 6, 1.0, None, 10),
    ("ambient", 12, 7, 1.0, None, None),
]


def build(seed):
    p = bs.building_params(seed)
    az_parts = [[], []]
    gx_parts = [[], []]
    bounds = []
    sf = 0
    for name, nw, exc, amp, imp, shf in SCRIPT:
        sig = bs.simulate_batch([p], exc, nw, None, "default", amp)[0]
        az, gx = bs.sensor_output(p, sig, exc, impact_floor=imp, shift_floor=shf)
        for i, f in enumerate(FLOORS):
            az_parts[i].append(bs.to_windows(az[f - 1]))
            gx_parts[i].append(bs.to_windows(gx[f - 1]))
        bounds.append((name, sf, sf + nw - 1))
        sf += nw
    az = [np.concatenate(a) for a in az_parts]
    gx = [np.concatenate(g) for g in gx_parts]
    return az, gx, bounds


def write(path, az, gx):
    n = az[0].shape[0]
    with open(path, "wb") as f:
        f.write(b"RTRC" + struct.pack("<III", 1, 2, n))
        for i in range(2):
            w = np.concatenate([az[i], gx[i]], axis=1).astype("<i2")
            assert w.shape == (n, 256)
            f.write(w.tobytes())
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    az, gx, bounds = build(a.seed)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    n = write(a.out, az, gx)
    # sidecar read by netsim: the physical floor of slave 1 and 2 (meta.floors in the schema-2 event log)
    Path(str(a.out) + ".floors").write_text(f"{FLOORS[0]},{FLOORS[1]}" + chr(10))
    print(f"wrote {a.out}: {n} windows per slave, building seed {a.seed}, floors {FLOORS}")
    for name, s, e in bounds:
        print(f"  superframes {s:3d}-{e:3d}  {name}")


if __name__ == "__main__":
    main()
