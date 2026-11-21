"""Writes the netsim demo window traces: firmware/esp32/test/data/demo_traces.bin

One simulated building (building_sim), two slaves on floors 5 and 10, scripted as consecutive recordings (1 window = 1 superframe).
Scenario "damage" (default, Phase C2b, seed 5,990,011: f1 7.09 Hz, a storey-1 loss of 30 % moves it by 3.9 %):
  commissioning 20 | ambient 100 (f1 commissions on both slaves) | impact on slave 1 (floor 5) 12 | ambient 12 |
  3x building-wide 12 | ambient 12 | frequency shift on slave 2 (floor 10) 12 | ambient 12 | PERMANENT storey-1 stiffness
  loss of 30 % from superframe 192 to the end (128 windows, 320 in total).
Scenario "legacy" (the Phase C2a demo, seed 5,990,001, 116 windows, no damage): --scenario legacy --out FILE.
Each segment is a separate recording (new excitation realisation, same building and sensors).

Regenerate:   .venv/Scripts/python.exe firmware/esp32/tools/gen_traces.py [--scenario damage|legacy] [--seed N] [--out FILE]
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
FLOORS = (5, 10)
DAMAGE_STOREY, DAMAGE_LOSS = 1, 0.30
# (name, windows, excitation id, amplitude multiplier, impact floor, shift floor, damage (storey, loss) | None)
SCRIPTS = {
    "damage": (5_990_011, [
        ("commissioning", 20, 0, 1.0, None, None, None),
        ("ambient (f1 commissions)", 100, 1, 1.0, None, None, None),
        ("impact on slave 1 (floor 5)", 12, 2, 1.0, 5, None, None),
        ("ambient", 12, 3, 1.0, None, None, None),
        ("3x building-wide", 12, 16, 3.0, None, None, None),
        ("ambient", 12, 5, 1.0, None, None, None),
        ("frequency shift on slave 2 (floor 10)", 12, 18, 1.0, None, 10, None),
        ("ambient", 12, 7, 1.0, None, None, None),
        ("DAMAGE: storey 1 stiffness -30 %", 128, 8, 1.0, None, None, (DAMAGE_STOREY, DAMAGE_LOSS)),
    ]),
    "legacy": (5_990_001, [
        ("commissioning", 24, 0, 1.0, None, None, None),
        ("ambient", 20, 1, 1.0, None, None, None),
        ("impact on slave 1 (floor 5)", 12, 2, 1.0, 5, None, None),
        ("ambient", 12, 3, 1.0, None, None, None),
        ("3x building-wide", 12, 4, 3.0, None, None, None),
        ("ambient", 12, 5, 1.0, None, None, None),
        ("frequency shift on slave 2 (floor 10)", 12, 6, 1.0, None, 10, None),
        ("ambient", 12, 7, 1.0, None, None, None),
    ]),
}


def build(seed, script):
    p = bs.building_params(seed)
    az_parts = [[], []]
    gx_parts = [[], []]
    bounds = []
    sf = 0
    for name, nw, exc, amp, imp, shf, dmg in script:
        sig = bs.simulate_batch([p], exc, nw, [dmg], "default", amp)[0]
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
    ap.add_argument("--scenario", choices=sorted(SCRIPTS), default="damage")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    seed = a.seed if a.seed is not None else SCRIPTS[a.scenario][0]
    az, gx, bounds = build(seed, SCRIPTS[a.scenario][1])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    n = write(a.out, az, gx)
    # sidecar read by netsim: the physical floor of slave 1 and 2 (meta.floors in the schema-2 event log)
    Path(str(a.out) + ".floors").write_text(f"{FLOORS[0]},{FLOORS[1]}" + chr(10))
    print(f"wrote {a.out}: {n} windows per slave, building seed {seed}, floors {FLOORS}")
    for name, s, e in bounds:
        print(f"  superframes {s:3d}-{e:3d}  {name}")


if __name__ == "__main__":
    main()
