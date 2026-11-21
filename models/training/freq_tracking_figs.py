"""Figures for Phase A from models/selected/validation/freq_tracking.json -> docs/figures/*.png.
Run with the .venv-tinyml python (has matplotlib):  D:\\Resonance\\.venv-tinyml\\Scripts\\python.exe models/training/freq_tracking_figs.py [--json PATH] [--out DIR]
  f1_drop_vs_damage.png      eigen f1 drop vs storey stiffness loss, one line per storey (physics, no estimator)
  detection_vs_damage.png    TEST detection rate vs loss per storey (A = 0.5%, primary pair), 95% bootstrap bands over buildings
  f1_timeseries_example.png  one TEST building: per-slave f1 estimates, pooled drop with the delta band and the DAMAGE onset
"""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
COL = ["#2a6fbb", "#d9822b", "#3a9d5d", "#8c5bb5", "#c4423f", "#6b6b6b", "#1b998b", "#a0791c", "#444444", "#e377c2"]
INK, GRID = "#222222", "#dddddd"


def style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)


def fig_physics(R, out):
    pc = R["physics_curve"]
    fig, ax = plt.subplots(figsize=(7, 4.2))
    x = [0] + [100 * d for d in pc["loss"]]
    for i, s in enumerate(range(1, 11)):
        y = [0] + [100 * v for v in pc["drop_by_storey"][str(s)]]
        ax.plot(x, y, color=COL[i], lw=1.6, ls="-" if i < 5 else "--", label=f"storey {s}")
    ax.set_xlabel("storey stiffness loss (%)")
    ax.set_ylabel("drop of first natural frequency (%)")
    ax.set_title("Physics: damage near the top barely moves f1 (eigen solution, no estimator)", fontsize=10, color=INK)
    ax.legend(ncol=2, fontsize=8, frameon=False)
    style(ax)
    fig.tight_layout()
    fig.savefig(out / "f1_drop_vs_damage.png", dpi=150)
    plt.close(fig)


def fig_detection(R, out):
    rp = R["test"]["A0.5_primary"]
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for i, s in enumerate(sorted({c["storey"] for c in rp["cells"].values()})):
        cs = sorted((c for c in rp["cells"].values() if c["storey"] == s), key=lambda c: c["loss"])
        x = [100 * c["loss"] for c in cs]
        y = [c["det"]["value"] for c in cs]
        lo = [c["det"]["ci95"][0] for c in cs]
        hi = [c["det"]["ci95"][1] for c in cs]
        ax.fill_between(x, lo, hi, color=COL[i], alpha=0.12, lw=0)
        ax.plot(x, y, color=COL[i], lw=1.8, marker="o", ms=4, label=f"storey {s}")
    ax.set_ylim(-0.02, 1.02)
    ax.set_xlabel("storey stiffness loss (%)")
    ax.set_ylabel("DAMAGE detection rate (test)")
    ch = R["selected"]
    fa = rp["fa_per_24h"]["value"]
    ax.set_title(f"Detection vs damage, A_env 0.5%, floors 5/10 (M={ch['M']}, C={ch['C']}, delta={100 * ch['delta']:.2f}%, P={ch['P']}; false {fa:.2f}/24 h)", fontsize=9, color=INK)
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    style(ax)
    fig.tight_layout()
    fig.savefig(out / "detection_vs_damage.png", dpi=150)
    plt.close(fig)


def fig_example(R, out):
    e = R["example"]
    t0 = e["damage_start_window"]
    est = np.array(e["f1_est"], float)
    pooled = np.array([np.nan if v is None else v for v in e["pooled_drop"]], float)
    st = np.array(e["damage_state"])
    n = len(pooled)
    tmin = np.arange(n) * e["superframe_s"] / 60
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    for i, fl in enumerate(e["f1_est_slave_floors"]):
        a1.plot(tmin, est[i], color=COL[i], lw=1.2, label=f"slave floor {fl}: f1 estimate")
    a1.axhline(e["f1_true_baseline"], color=INK, lw=0.8, ls=":", label="true f1 (undamaged)")
    a1.axhline(e["f1_true_damaged"], color=INK, lw=0.8, ls="--", label="true f1 (damaged)")
    a1.set_ylabel("f1 (Hz)")
    a1.legend(fontsize=8, frameon=False)
    fig.text(0.01, 0.005, "Example cell switched from (storey 3, 20%) to (storey 3, 30%) after the test example was seen (no onset at 20%): presentation only.", fontsize=7, color=INK)
    a1.set_title(f"Example: building {e['seed']}, storey {e['storey']} loses {100 * e['loss']:.0f}% at the dashed line (M={e['M']}, C={e['C']})", fontsize=10, color=INK)
    d = e["delta"]
    a2.axhspan(d / 2, d, color=COL[1], alpha=0.15, lw=0, label="hysteresis band (delta/2 .. delta)")
    a2.axhline(d, color=COL[1], lw=1.2, label=f"delta = {100 * d:.2f}%")
    a2.plot(tmin, pooled, color=COL[0], lw=1.4, label="pooled f1 drop")
    on = np.nonzero(st)[0]
    if len(on):
        a2.axvspan(tmin[on[0]], tmin[-1] if st[-1] else tmin[on[-1]], color=COL[4], alpha=0.10, lw=0, label="DAMAGE state")
        a2.axvline(tmin[on[0]], color=COL[4], lw=1.4)
    for a in (a1, a2):
        a.axvline(tmin[t0], color=INK, lw=0.8, ls="--")
        style(a)
    a2.set_ylabel("relative f1 drop")
    a2.set_xlabel("time (min, one window per 1.4 s superframe)")
    a2.legend(fontsize=8, frameon=False, loc="upper left")
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    fig.savefig(out / "f1_timeseries_example.png", dpi=150)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=ROOT / "models" / "selected" / "validation" / "freq_tracking.json")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "figures")
    a = ap.parse_args()
    R = json.loads(a.json.read_text(encoding="utf-8"))
    a.out.mkdir(parents=True, exist_ok=True)
    fig_physics(R, a.out)
    fig_detection(R, a.out)
    fig_example(R, a.out)
    print("wrote figures to", a.out)


if __name__ == "__main__":
    main()
