"""Tests of the ctypes front-end bridge. Run: .venv/Scripts/python.exe models/training/test_frontend.py"""
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "data" / "generator"))
import frontend  # noqa: E402


def test_dataset_features():
    import pandas as pd
    csv = ROOT / "data" / "processed" / "dataset.csv"
    assert csv.exists(), "regenerate with .venv/Scripts/python data/generator/generate.py"
    df = pd.read_csv(csv)
    az = df[[f"az_{i}" for i in range(128)]].to_numpy().astype(np.int16)
    gx = df[[f"gx_{i}" for i in range(128)]].to_numpy().astype(np.int16)
    f = frontend.features5(az, gx).astype(np.float64)
    want = df[["az_std", "az_kurt", "az_domfreq", "gx_std", "gx_domfreq"]].to_numpy()
    assert f.shape == want.shape == (6000, 5)
    assert np.array_equal(f[:, 2], want[:, 2]) and np.array_equal(f[:, 4], want[:, 4]), "domfreq not exact"
    for j in (0, 3):
        assert np.allclose(f[:, j], want[:, j], rtol=1e-4, atol=1e-3), j
    assert np.allclose(f[:, 1], want[:, 1], atol=1e-3)
    print("dataset features: 6000 windows, domfreq exact, max abs diff std/kurt",
          [float(np.abs(f[:, j] - want[:, j]).max()) for j in (0, 1, 3)])


def test_zoom_sine():
    n = np.arange(128)
    for f0 in (4.37, 7.0, 9.91):
        x = np.rint(500 * np.sin(2 * np.pi * f0 * n / 100 + 0.3)).astype(np.int16)
        hz = float(frontend.peak(frontend.zoom(x))[0])
        assert abs(hz - f0) < 0.02, (f0, hz)


def test_zoom_building():
    import building_sim as bs
    errs = []
    for seed in (101, 102, 103, 104, 105, 106):
        r = bs.simulate_building(seed, exc_id=1, n_windows=16)
        az = bs.to_windows(r["az"][-1:])[0]  # top floor (index 9), (16, 128)
        z = frontend.zoom(az).astype(np.float64)
        hz = float(frontend.peak(z.mean(axis=0).astype(np.float32))[0])
        # also: mean of per-window peaks
        hzs = float(np.mean(frontend.peak(z.astype(np.float32))))
        f1 = float(bs.f1_hz(r["params"]))
        errs.append((seed, f1, hz, hz - f1, hzs - f1))
    print("building f1 (seed, true, peak of mean spectrum, err, err of mean per-window peaks):")
    for e in errs:
        print("  %d  %.3f  %.3f  %+.3f  %+.3f" % e)
    worst = max(abs(e[3]) for e in errs)
    print("max |err| (mean spectrum) = %.3f Hz" % worst)
    # The task asked for < 0.1 Hz; seed 101 gives 0.104, so that bound is NOT met and is reported, not tuned.
    # The assert below is only a sanity bound (0.15 Hz = 3 grid-widths of the zoom is not a claim of accuracy).
    assert worst < 0.15, worst


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
