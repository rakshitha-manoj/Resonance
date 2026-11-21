"""Writes models/tinyml/ae_report.json and ae_report.md: model facts, quantisation degradation, the design-search evidence and the
benchmark table (decision tree / rule b / Mahalanobis / int8 AE: params, flash bytes of weights, RAM, op count, host time per window,
dev metrics). Main venv; run after fusion_eval_r3.py:  python models/training/ae_report.py
Host times: the C code through ctypes on this PC (gcc -O2 -ffp-contract=off), amortised over 20,000 windows; they are NOT ESP32
times (the on-device numbers come from `pio run -e selftest` in Wokwi / on hardware: `ae us ...` line)."""
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import ae_bridge  # noqa: E402
import frontend  # noqa: E402
import fusion_eval as fe  # noqa: E402

ROOT = fe.ROOT
TINY = ROOT / "models" / "tinyml"
R3 = fe.OUT_DIR / "fusion_eval_r3_dev.json"


def layer_dims():
    h = (ROOT / "firmware/esp32/include/ae_model.h").read_text()
    return [(int(a), int(b)) for a, b in re.findall(r"\{(\d+), (\d+), ae_w\d", h)]


def timeit(fn, reps=3):
    best = 1e9
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def main():
    num = json.loads((TINY / "ae_numbers.json").read_text())
    ds = json.loads((TINY / "design_search.json").read_text())
    mah = json.loads((TINY / "mahalanobis.json").read_text())
    r3 = json.loads(R3.read_text())
    dims = layer_dims()
    macs = sum(i * o for i, o in dims)
    nb = sum(o for _, o in dims)
    flash_ae = macs + 4 * nb * 2 + nb      # int8 weights + int32 bias + int32 multiplier + int8 shift
    nin = dims[0][0]
    # host timing
    rng = np.random.default_rng(0)
    n = 20000
    az = rng.integers(15000, 17000, (n, 128)).astype(np.int16)
    gx = rng.integers(-50, 50, (n, 128)).astype(np.int16)
    t_base = timeit(lambda: ae_bridge.base20(az, gx)) / n
    t_feat5 = timeit(lambda: frontend.features5(az[:5000], gx[:5000])) / 5000
    b = ae_bridge.base20(az[:2000], gx[:2000])
    comm = b[:20]
    t_ae = timeit(lambda: ae_bridge.eval_node(comm, np.tile(b, (10, 1)))) / (len(b) * 10)
    z = rng.normal(size=(20000, nin))
    mu, P = np.array(mah["mean"]), np.array(mah["precision"])
    t_mah = timeit(lambda: np.einsum("ij,jk,ik->i", z - mu, P, z - mu)) / len(z)
    ref = {r["name"]: r for r in json.loads((fe.OUT_DIR / "fusion_eval.json").read_text())["rounds"]["R2b"]["development_table_primary_pair"]}
    tree_rows = [r for r in ref.values() if r["rule"] == "a"]
    tree_best = min(tree_rows, key=lambda r: r["false_onsets_per_hour"])
    bf = r3["best_per_family"]
    def met(r):
        return (f"{r['false_onsets_per_hour']:.2f}", f"{r['det_local_recall']:.3f}", f"{r['det_building_recall']:.3f}", f"{r['recall_det_shift']:.3f}", f"{r['recall_det_sparse']:.3f}")
    rows = [
        ("decision tree (depth 3, rule a, lowest-false-onset row)", "11 nodes", 110, "5 floats", "~3 compares (+ 2 DFTs of the front end, 32,768 MAC)", f"features5 {t_feat5 * 1e6:.1f} us", tree_best["name"], met(tree_best)),
        ("rule b (az_std / baseline >= T, fusion)", "1 baseline", 4, "4 B baseline + 3 flags", "1 divide + compare (+ std pass, ~400 flops)", "< 1 us (std only)", bf["b"]["name"], met(bf["b"])),
        ("Mahalanobis (shrinkage %.2f), rule m" % mah["shrinkage"], f"{nin} + {nin * (nin + 1) // 2}", 4 * (nin + nin * (nin + 1) // 2), "~1.8 KB node state (shared with AE)", f"~{nin * nin + 2 * nin} MAC (float)", f"{t_mah * 1e6:.2f} us (numpy batch)", bf["m"]["name"], met(bf["m"])),
        ("int8 autoencoder %s, rule e" % "-".join(str(d) for d in [dims[0][0]] + [o for _, o in dims[:-1]] + [dims[-1][1]]), f"{num['params']}", flash_ae, "~1.8 KB node state + 40 B scratch", f"{macs} int8 MAC", f"{t_ae * 1e6:.2f} us (norm+quant+AE+score)", bf["e"]["name"], met(bf["e"])),
        ("rule f = b OR e", "-", flash_ae + 4, "as AE", f"{macs} int8 MAC + b", "as e", bf["f"]["name"], met(bf["f"])),
        ("rule g = b OR m", "-", 4 * (nin + nin * (nin + 1) // 2) + 4, "as m", "as m + b", "as m", bf["g"]["name"], met(bf["g"])),
    ]
    out = {"model": num, "mahalanobis": {k: mah[k] for k in ("shrinkage", "val_recall_at_0.5pct")}, "host_us_per_window": {"base20_frontend": t_base * 1e6, "features5": t_feat5 * 1e6, "ae_stage": t_ae * 1e6, "mahalanobis_numpy": t_mah * 1e6},
           "ae_flash_bytes": flash_ae, "ae_macs": macs, "benchmark_rows": [dict(zip(["detector", "params", "flash_B", "RAM", "ops", "host_time", "dev_config", "dev_metrics"], r)) for r in rows]}
    (TINY / "ae_report.json").write_text(json.dumps(out, indent=1))
    md = ["# int8 autoencoder (Phase B): report", "",
          "Healthy-data-only anomaly detector for each sensor node. All numbers: simulation (`building_sim`), nothing measured on hardware.", "",
          "## Model", "",
          f"- architecture {dims[0][0]}-{'-'.join(str(o) for _, o in dims)} (ReLU, dense), design A input (16 az log band-powers, az_std, az_kurt, gx_std, gx_kurt), per-node normalisation learned on the node from the 20 commissioning windows (median / 1.4826 MAD, floor = {T_floor()} x the training median MAD per input)",
          f"- parameters {num['params']}; int8 TFLite file {num['int8_tflite_bytes']} bytes; firmware weights+quantisation tables {flash_ae} bytes ({macs} int8 MACs per window)",
          "- training: seeds 6,300,000-6,300,399 (400 buildings x 10 floors; commissioning + default and broadband ambient at level 0.5-1.5x, 220 windows per node, 880,000 windows), validation 6,300,400-6,300,499; Adam, MSE, 30 epochs; full-integer post-training quantisation, 3000 representative training vectors",
          f"- score = MSE in normalised space; ratio = score / median score of the 20 commissioning windows (the ratio is what is thresholded)", "",
          "## Float vs int8 (validation healthy windows)", "",
          f"- reconstruction MSE: float {num['float_recon_mse_val_healthy']:.4f}, int8 {num['int8_recon_mse_val_healthy']:.4f} (+{100 * (num['int8_recon_mse_val_healthy'] / num['float_recon_mse_val_healthy'] - 1):.1f}%); median {num['recon_mse_median_float']:.4f} vs {num['recon_mse_median_int8']:.4f}",
          f"- per-window score: mean relative difference {100 * num['score_rel_diff_mean']:.1f}%, correlation {num['score_corr']:.4f}",
          f"- quantisation degradation of detection (validation, window level at the healthy 0.5% false-positive threshold): mean recall float {num['val_recall_float']['mean']:.3f} vs int8 {num['val_recall_int8']['mean']:.3f} (impacts {num['val_recall_float']['imp']:.3f}/{num['val_recall_int8']['imp']:.3f}, sparse {num['val_recall_float']['sp']:.3f}/{num['val_recall_int8']['sp']:.3f}, shift {num['val_recall_float']['shift']:.3f}/{num['val_recall_int8']['shift']:.3f}, x3 {num['val_recall_float']['x3']:.3f}/{num['val_recall_int8']['x3']:.3f})",
          "- C kernel vs `tf.lite.Interpreter` (BUILTIN_REF): 0 mismatching int8 outputs on 10,000 vectors (200,000 values); optimised and reference interpreters agree too. TFLite in this build rounds ONCE in MultiplyByQuantizedMultiplier (single-rounding form); the gemmlowp double-rounding form differs in 28% of the vectors.", "",
          "## Input design and architecture (validation split only, `design_search.json`)", "",
          "Window-level recall of the anomaly windows at the threshold that gives 0.5% false positives on healthy validation windows (levels 0.5-1.5x); mean of impact / sparse impact / frequency shift / 3x building-wide; two training seeds each.", "",
          "| design | hidden | seed | params | healthy p99 ratio | impact | sparse | shift | 3x | mean |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in ds["results"]:
        c = r["recall"]["fpr0.005"]
        md.append(f"| {r['design']} | {r['hidden']} | {r['seed']} | {r['params']} | {r['healthy_p99']:.2f} | {c['imp']:.2f} | {c['sp']:.2f} | {c['shift']:.2f} | {c['x3']:.2f} | {c['mean']:.3f} |")
    md += ["", "Designs: A = the 20 base inputs; B = shape only (band log-powers minus their mean, + the two kurtoses); C = B + log(gx_std/az_std); D = A with log std.",
           "Findings: (1) the shape-only designs (B, C) did NOT lower the healthy false-positive tail (p99 ratio 4.9-5.0 vs 4.3-4.9 for A) and lost most of the frequency-shift and amplitude sensitivity, so design A was kept. (2) Smaller bottlenecks and hidden layers generalise less to the anomalies: 16-6-16 and 24-8-24 are clearly worse (mean 0.47-0.49), 8-3-8 / 12-2-12 / 12-4-12 are within the seed noise (0.58-0.63); 12-2-12 had the best two-seed mean and was taken. (3) Because the training data contains the 0.5-1.5x level changes, a building-wide amplitude change is largely reconstructed (3x recall 0.1): the AE does not replace rule b for amplitude events.", "",
           f"Mahalanobis baseline on the same normalised inputs: covariance of the 880,000 training windows, shrinkage towards (trace/n) I chosen on validation = {mah['shrinkage']}, validation mean recall {mah['val_recall_at_0.5pct']['mean']:.3f} (AE int8 {num['val_recall_int8']['mean']:.3f}).", "",
           "## Benchmark (development block 6,100,000+240, primary pair, best configuration of each family under 1.0 false onsets/h)", "",
           "| detector | params | flash bytes (weights) | RAM | ops per window | host time per window | dev config | false onsets/h | LOCAL | BUILDING | shift | sparse |", "|" + "---|" * 12]
    for r in rows:
        md.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} | " + " | ".join(r[7]) + " |")
    md += ["", f"Host: front end (az DFT, 16 bands, std/kurt) {t_base * 1e6:.1f} us per window; features5 (tree features, two DFTs) {t_feat5 * 1e6:.1f} us. ESP32 timings: see the `ae us` line of the selftest (Wokwi / hardware). The tree row shows the lowest-false-onset rule-a configuration of R2b (the tree-based rules do not meet the budget). Dev metrics are from `models/selected/validation/fusion_eval_r3_dev.md`.", ""]
    (TINY / "ae_report.md").write_text("\n".join(md), encoding="utf-8")
    print("wrote ae_report")


def T_floor():
    return json.loads((TINY / "ae_norm.json").read_text())["floor_frac"]


if __name__ == "__main__":
    main()
