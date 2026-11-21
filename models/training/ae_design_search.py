"""Input-design and architecture search for the autoencoder, on the VALIDATION split of the AE training block only
(seeds 6,300,400-6,300,499; training on 6,300,000-6,300,399). The dev block 6,100,000+ is never loaded.
Metric (window level, per node): score ratio = MSE / median MSE of the node's 20 commissioning windows. Threshold T* = the
healthy 99.5th percentile (default-excitation healthy recordings of all validation nodes, levels 0.5-1.5x); recall of the
anomaly windows at T* (event windows on the event floor; x3 = every window of floors 5 and 10), per kind and their mean.
Also the healthy ratio's 99th / 99.9th percentile (spread caused by the level changes) and the level robustness: the median
ratio of healthy windows at level multiplier < 0.7 and > 1.3 is not available per window, so the p99.9/p50 spread is used.
Run (tinyml venv): python models/training/ae_design_search.py   -> models/tinyml/design_search.json
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ae_train as T  # noqa: E402

OUT = T.ROOT / "models" / "tinyml"


def prep(design, tr, va):
    comm_tr = T.make_input(tr["comm"], design)            # (nb,10,20,n_in)
    floor = (T.FLOOR_FRAC * T.unfloored_scale_median(comm_tr)).astype(np.float32)
    def zof(D, key):
        med, sc = T.node_stats(T.make_input(D["comm"], design), floor)
        return T.normalise(T.make_input(D[key], design), med, sc)
    return floor, zof


def evaluate(model, floor, zof, va):
    comm_mse = T.mse_rows(model, zof(va, "comm"))                 # (nb,10,20)
    ref = np.maximum(np.median(comm_mse, axis=-1), 1e-6)[..., None]
    ratio = lambda key: T.mse_rows(model, zof(va, key)) / ref  # noqa: E731
    hl, bb = ratio("hl"), ratio("bb")
    res = {"healthy_p50": float(np.percentile(hl, 50)), "healthy_p99": float(np.percentile(hl, 99)), "healthy_p999": float(np.percentile(hl, 99.9)),
           "broadband_p50": float(np.percentile(bb, 50)), "broadband_p99": float(np.percentile(bb, 99))}
    out = {}
    for fpr in (0.005, 0.001):
        thr = float(np.percentile(hl, 100 * (1 - fpr)))
        rec = {}
        for kind in ("imp", "sp", "shift"):
            hit = tot = 0
            for f in (5, 10):
                r = ratio(f"{kind}{f}")[:, f - 1]                  # (nb, 30) event floor
                m = va[f"{kind}{f}_mask"]
                hit += int(((r >= thr) & m).sum())
                tot += int(m.sum())
            rec[kind] = hit / max(tot, 1)
        r3 = ratio("x3")[:, [4, 9]]
        rec["x3"] = float((r3 >= thr).mean())
        rec["mean"] = float(np.mean([rec[k] for k in ("imp", "sp", "shift", "x3")]))
        out[f"fpr{fpr}"] = dict(rec, threshold=thr)
    res["recall"] = out
    return res


def main():
    tr, va = T.load("train"), T.load("val")
    rng = np.random.default_rng(0)
    results = []
    def run(design, hidden, epochs=20, seed=0, tag=""):
        t = time.time()
        floor, zof = prep(design, tr, va)
        ztr = np.concatenate([zof(tr, k).reshape(-1, floor.size) for k in ("comm", "hl", "bb")])
        ztr = ztr[rng.choice(len(ztr), min(len(ztr), 300_000), replace=False)]
        zv = zof(va, "hl").reshape(-1, floor.size)
        m = T.build_model(floor.size, hidden, seed)
        T.fit(m, ztr, zv, epochs=epochs)
        r = evaluate(m, floor, zof, va)
        r.update(design=design, hidden=list(hidden), epochs=epochs, seed=seed, n_in=int(floor.size), params=int(m.count_params()), seconds=round(time.time() - t))
        results.append(r)
        print(design, hidden, seed, "p99 %.2f p999 %.2f | recall@0.5%% imp %.2f sp %.2f shift %.2f x3 %.2f mean %.3f" % (
            r["healthy_p99"], r["healthy_p999"], *(r["recall"]["fpr0.005"][k] for k in ("imp", "sp", "shift", "x3", "mean"))), flush=True)
    for d in "ABCD":
        for s in (0, 1):
            run(d, (12, 4, 12), seed=s)
    best = max("ABCD", key=lambda d: np.mean([r["recall"]["fpr0.005"]["mean"] for r in results if r["design"] == d]))
    print("best design", best)
    for hid in ((8, 3, 8), (16, 6, 16), (24, 8, 24), (12, 2, 12), (12, 6, 12)):
        for s in (0, 1):
            run(best, hid, seed=s)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "design_search.json").write_text(json.dumps({"best_design_by_mean_recall": best, "results": results}, indent=1))


if __name__ == "__main__":
    main()
