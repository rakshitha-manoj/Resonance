"""T1: localisation gate (Part A), tree behaviour per floor (Part B), noise gate value (Part C).

Method, thresholds-rule, excitation and pass criterion are fixed in this file BEFORE any result existed.
Writes models/selected/validation/localisation_gate.json and prints GATE: PASS / GATE: FAIL.
"""
import json
import re
import sys
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "data" / "generator"))
import building_sim as bs  # noqa: E402

T0 = time.time()
OUT = ROOT / "models" / "selected" / "validation" / "localisation_gate.json"
FS, N, NF = 100, 128, 10
BIN_HZ = 0.78125
NW = 30  # windows per assessment
EPS = 1.0  # counts^2 floor inside the noise-corrected amplitude
CLASSES = ["normal", "abnormal", "faulty"]
USED = ["az_std", "az_kurt", "az_domfreq", "gx_std", "gx_domfreq"]

# fixed pass criterion (30% loss, default excitation)
PASS_TOP1, PASS_WITHIN1, PASS_FA = 0.70, 0.90, 0.05
FA_ALPHA_PCT = 95.0

# building-seed blocks (disjoint by construction, asserted)
BASE = {"calib": 100_000, "score30": 200_000, "score15": 300_000, "fa": 400_000, "b_calib": 500_000, "b_score30": 600_000,
        "b_score15": 700_000, "b_fa": 800_000, "partB": 900_000, "noise": 950_000, "expl1": 1_000_000, "expl2": 1_100_000}
N_CALIB, N_FA, N_PER_CELL, N_B_CALIB, N_B_FA = 500, 1500, 100, 300, 600
N_PARTB = 200
EXC_BASELINE, EXC_ASSESS, EXC_NOISE = 0, 1, 2


def log(*a):
    print(*a, flush=True)


def seeds_block(name, n):
    return list(range(BASE[name], BASE[name] + n))


# ------------------------------------------------------------------ features and tree
def window_features(x):
    """x (..., 128) int -> std, excess kurtosis, domfreq per feature_spec.json formulas (float64)."""
    x = x.astype(np.float64)
    xc = x - x.mean(axis=-1, keepdims=True)
    var = (xc ** 2).mean(axis=-1)
    std = np.sqrt(var)
    kurt = np.where(var > 0, (xc ** 4).mean(axis=-1) / np.where(var > 0, var, 1) ** 2 - 3, 0.0)
    mag = np.abs(np.fft.rfft(xc, axis=-1))
    dom = (np.argmax(mag[..., 1:], axis=-1) + 1) * FS / N
    return std, kurt, dom


def feats5(az_w, gx_w):
    a_std, a_kurt, a_dom = window_features(az_w)
    g_std, _, g_dom = window_features(gx_w)
    return {"az_std": a_std, "az_kurt": a_kurt, "az_domfreq": a_dom, "gx_std": g_std, "gx_domfreq": g_dom}


class Tree:
    def __init__(self):
        spec = json.loads((ROOT / "models/selected/feature_spec.json").read_text())
        exp = json.loads((ROOT / "models/selected/model_export.json").read_text())
        assert spec["used_feature_columns"] == USED and exp["used_feature_names"] == USED
        t = exp["trees"][0]
        self.names = t["feature_name"]
        self.thr = np.array([np.float32(v) if v is not None else np.float32(0) for v in t["threshold"]], dtype=np.float32)
        self.left, self.right = np.array(t["left"]), np.array(t["right"])
        self.leaf = np.array([-1 if v is None else v for v in t["leaf_class"]])
        self.col = np.array([USED.index(n) if n else 0 for n in self.names])
        self.exp_all = exp
        self.model = joblib.load(ROOT / "models/selected/model.joblib")
        h = (ROOT / "firmware/esp32/include/model.h").read_text()
        body = h[h.index("static inline int predict"):]
        self.h_thr = {m.group(1): np.float32(float.fromhex(m.group(2))) for m in re.finditer(r"if \((\w+) <= (-?0x[0-9a-fA-F.]+p[+-]\d+)f\)", body)}
        assert set(self.h_thr) == {"az_std", "az_domfreq", "gx_std", "az_kurt", "gx_domfreq"}, self.h_thr
        self.thr_az_std = float(self.h_thr["az_std"])
        self.thr_kurt = float(self.h_thr["az_kurt"])

    def predict(self, f):
        X = np.stack([np.asarray(f[n], dtype=np.float32).ravel() for n in USED], axis=1)
        node = np.zeros(len(X), dtype=int)
        for _ in range(8):
            internal = self.left[node] >= 0
            if not internal.any():
                break
            go_left = X[np.arange(len(X)), self.col[node]] <= self.thr[node]
            node = np.where(internal, np.where(go_left, self.left[node], self.right[node]), node)
        return self.leaf[node].reshape(np.shape(f["az_std"]))

    def predict_model_h(self, f):
        """Transcription of predict() in firmware/esp32/include/model.h using its parsed hex-float constants."""
        h = self.h_thr
        a = lambda n: np.asarray(f[n], dtype=np.float32)
        left = a("az_std") <= h["az_std"]
        l1 = np.where(a("az_domfreq") <= h["az_domfreq"], np.where(a("gx_std") <= h["gx_std"], 0, 1), 2)
        r1 = np.where(a("az_kurt") <= h["az_kurt"], np.where(a("gx_domfreq") <= h["gx_domfreq"], 1, 2), 2)
        return np.where(left, l1, r1)

    def predict_joblib(self, f):
        X = np.zeros((np.size(f["az_std"]), 18))
        idx = {"az_std": 6, "az_kurt": 7, "az_domfreq": 8, "gx_std": 9, "gx_domfreq": 11}
        for n, i in idx.items():
            X[:, i] = np.asarray(f[n]).ravel()
        return self.model.predict(X)


def smooth_sequences(raw):
    """2-of-3 smoothing of smooth.h, vectorised over sequences. raw (S, T) -> state (S, T). hist {-1,-1,-1}, state 0."""
    S, T = raw.shape
    hist = -np.ones((S, 3), dtype=int)
    state = np.zeros(S, dtype=int)
    out = np.empty_like(raw)
    for t in range(T):
        hist = np.concatenate([hist[:, 1:], raw[:, t:t + 1]], axis=1)
        new = state.copy()
        done = np.zeros(S, dtype=bool)
        for c in range(3):
            hit = ((hist == c).sum(axis=1) >= 2) & ~done
            new[hit] = c
            done |= hit
        state = new
        out[:, t] = state
    return out


def smooth_scalar(raw):
    hist, state, out = [-1, -1, -1], 0, []
    for r in raw:
        hist = [hist[1], hist[2], int(r)]
        for c in range(3):
            if sum(h == c for h in hist) >= 2:
                state = c
                break
        out.append(state)
    return out


def tree_check(tree, wdf_sample):
    df = pd.read_csv(ROOT / "data/processed/dataset.csv")
    assert len(df) == 6000
    raw_cols = [f"{a}_{i}" for a in ("az", "gx") for i in range(N)]
    # my feature code reproduces the CSV features
    idx = np.arange(0, 6000, 7)
    az_w = df[[f"az_{i}" for i in range(N)]].to_numpy()[idx]
    gx_w = df[[f"gx_{i}" for i in range(N)]].to_numpy()[idx]
    fmine = feats5(az_w, gx_w)
    for n in USED:
        assert np.allclose(fmine[n], df[n].to_numpy()[idx], rtol=1e-9, atol=1e-9), n
    fcsv = {n: df[n].to_numpy() for n in USED}
    p_exp, p_h, p_job = tree.predict(fcsv), tree.predict_model_h(fcsv), tree.predict_joblib(fcsv)
    assert np.array_equal(p_exp, p_job) and np.array_equal(p_exp, p_h)
    y = df["label"].map({c: i for i, c in enumerate(CLASSES)}).to_numpy()
    f = {k: v.ravel() for k, v in wdf_sample.items()}
    assert np.array_equal(tree.predict(f), tree.predict_model_h(f)) and np.array_equal(tree.predict(f), tree.predict_joblib(f))
    rng = np.random.default_rng(1)
    for _ in range(200):
        seq = rng.integers(0, 3, rng.integers(1, 40))
        assert smooth_scalar(seq) == smooth_sequences(seq[None, :])[0].tolist()
    # thresholds in the export equal the ones in model.h
    for n, v in tree.h_thr.items():
        i = tree.names.index(n)
        assert tree.thr[i] == v, (n, tree.thr[i], v)
    return {"dataset_windows_checked": 6000, "feature_code_vs_csv_windows": int(len(idx)), "export_vs_joblib_vs_model_h_identical": True,
            "simulated_windows_checked": int(len(f["az_std"])), "dataset_accuracy_vs_labels_in_sample": float((p_exp == y).mean()), "smoothing_python_vs_scalar_port": "identical on 200 random sequences"}, df


# ------------------------------------------------------------------ Part A
def sensor_windows(params, sig, exc_id, impact_floors=None):
    az = np.empty((len(params), NF, sig.shape[-1]), dtype=np.int16)
    gx = np.empty_like(az)
    for i, p in enumerate(params):
        az[i], gx[i] = bs.sensor_output(p, sig[i], exc_id, None if impact_floors is None else impact_floors[i])
    return bs.to_windows(az), bs.to_windows(gx)


def mean_var(az_w):
    return (az_w.astype(np.float64).std(axis=-1) ** 2).mean(axis=-1)  # mean(az_std^2) per floor, (B, 10)


def ratios(az_w, noise_var):
    s = np.sqrt(np.maximum(mean_var(az_w) - noise_var, EPS))
    r = np.empty_like(s)
    r[:, 0] = s[:, 0] / np.median(s, axis=1)
    r[:, 1:] = s[:, 1:] / s[:, :-1]
    return r


def assess(seeds, damages, excitation="default", n_windows=NW, chunk=bs.BATCH):
    """Returns DI (B,10) = log(r/r0) for each building."""
    DI = []
    for i in range(0, len(seeds), chunk):
        ps = [bs.building_params(s) for s in seeds[i:i + chunk]]
        dm = damages[i:i + chunk]
        zero = np.zeros((len(ps), NF, n_windows * N))
        naz, _ = sensor_windows(ps, zero, EXC_NOISE)
        nv = mean_var(naz)
        base = sensor_windows(ps, bs.simulate_batch(ps, EXC_BASELINE, n_windows, None, excitation), EXC_BASELINE)[0]
        ass = sensor_windows(ps, bs.simulate_batch(ps, EXC_ASSESS, n_windows, dm, excitation), EXC_ASSESS)[0]
        DI.append(np.log(ratios(ass, nv) / ratios(base, nv)))
    return np.concatenate(DI)


def score(DI, true_storey, thr):
    pred = np.argmax(np.abs(DI), axis=1) + 1
    alarm = np.abs(DI).max(axis=1) > thr
    top1, w1 = pred == true_storey, np.abs(pred - true_storey) <= 1
    out = {"n": int(len(pred)), "top1": float(top1.mean()), "within1": float(w1.mean()), "detect": float(alarm.mean()),
           "top1_given_alarm": float(top1[alarm].mean()) if alarm.any() else None, "per_storey": {}}
    for s in range(1, 11):
        m = true_storey == s
        out["per_storey"][s] = {"n": int(m.sum()), "top1": float(top1[m].mean()), "within1": float(w1[m].mean()), "detect": float(alarm[m].mean())}
    out["predicted_storey_histogram"] = np.bincount(pred, minlength=11)[1:].tolist()
    return out


def run_localisation(tag, excitation, n_calib, n_fa, names, extra_levels_seed_names=None):
    seeds_c = seeds_block(names["calib"], n_calib)
    DIc = assess(seeds_c, [None] * n_calib, excitation)
    thr = float(np.percentile(np.abs(DIc).max(axis=1), FA_ALPHA_PCT))
    seeds_f = seeds_block(names["fa"], n_fa)
    DIf = assess(seeds_f, [None] * n_fa, excitation)
    fa = float((np.abs(DIf).max(axis=1) > thr).mean())
    res = {"excitation": excitation, "threshold_p95_max_abs_DI": thr, "n_calibration_buildings": n_calib,
           "calibration_alarm_rate_at_threshold": float((np.abs(DIc).max(axis=1) > thr).mean()),
           "n_healthy_assessments": n_fa, "false_alarm_rate": fa,
           "healthy_max_abs_DI_percentiles": {str(q): float(np.percentile(np.abs(DIf).max(axis=1), q)) for q in (50, 90, 95, 99)},
           "levels": {}}
    f1shift = {}
    for lev, key in ((0.30, names["s30"]), (0.15, names["s15"])):
        seeds, dmg, true = [], [], []
        for s in range(1, 11):
            ss = seeds_block(key, 10 * N_PER_CELL)[(s - 1) * N_PER_CELL:s * N_PER_CELL]
            seeds += ss
            dmg += [(s, lev)] * N_PER_CELL
            true += [s] * N_PER_CELL
        DI = assess(seeds, dmg, excitation)
        res["levels"][f"{int(round(lev * 100))}%"] = score(DI, np.array(true), thr)
        d = np.array([bs.f1_hz(bs.building_params(sd), dm) - bs.f1_hz(bs.building_params(sd)) for sd, dm in zip(seeds, dmg)])
        rel = d / np.array([bs.f1_hz(bs.building_params(sd)) for sd in seeds])
        t = np.array(true)
        f1shift[f"{int(round(lev * 100))}%"] = {
            "mean_shift_hz_by_storey": {s: float(d[t == s].mean()) for s in range(1, 11)},
            "mean_relative_shift_pct_by_storey": {s: float(100 * rel[t == s].mean()) for s in range(1, 11)},
            "mean_abs_shift_hz_overall": float(np.abs(d).mean()), "max_abs_shift_hz": float(np.abs(d).max()),
            "bin_hz": BIN_HZ, "frac_buildings_abs_shift_over_one_bin": float((np.abs(d) > BIN_HZ).mean()),
            "frac_buildings_abs_shift_over_half_bin": float((np.abs(d) > BIN_HZ / 2).mean()),
            "mean_abs_shift_over_bin": float(np.abs(d).mean() / BIN_HZ)}
    res["f1_shift"] = f1shift
    log(f"  [{tag}] thr={thr:.4f} FA={fa:.4f} top1@30={res['levels']['30%']['top1']:.3f} top1@15={res['levels']['15%']['top1']:.3f}  t={time.time() - T0:.0f}s")
    return res


# ------------------------------------------------------------------ Part B
def class_table(pred, mask=None):
    """pred (B,10,T) -> fractions per floor (10,3)."""
    out = np.zeros((NF, 3))
    for k in range(NF):
        v = pred[:, k, :] if mask is None else pred[:, k, :][mask[:, k, :]]
        out[k] = np.bincount(v.ravel(), minlength=3) / max(v.size, 1)
    return out


def tables(t):
    return {f"floor{k + 1}": {c: float(t[k, i]) for i, c in enumerate(CLASSES)} for k in range(NF)}


def scenario(tree, seeds, kind, amp_mult=1.0):
    ps = [bs.building_params(s) for s in seeds]
    B = len(ps)
    imp = None
    dmg = None
    if kind == "damage30":
        dmg = [((i % 10) + 1, 0.30) for i in range(B)]
    if kind == "impact":
        imp = [(i % 10) + 1 for i in range(B)]
    if kind == "noise":
        sig = np.zeros((B, NF, NW * N))
    else:
        sig = bs.simulate_many(ps, 11, NW, dmg, "default", amp_mult)
    az, gx = sensor_windows(ps, sig, 11, imp)
    f = feats5(az, gx)
    pred = tree.predict(f).reshape(B, NF, NW)
    sm = smooth_sequences(pred.reshape(-1, NW)).reshape(B, NF, NW)
    return f, pred, sm, imp


def part_b(tree):
    seeds = seeds_block("partB", N_PARTB)
    R = {}
    keep = {}
    for name, kind, mult in (("healthy", "healthy", 1.0), ("damage_30pct", "damage30", 1.0), ("strong_2x", "healthy", 2.0),
                             ("strong_3x", "healthy", 3.0), ("strong_5x", "healthy", 5.0), ("noise_only", "noise", 1.0)):
        f, pred, sm, _ = scenario(tree, seeds, kind, mult)
        R[name] = {"raw": tables(class_table(pred)), "smoothed_2of3": tables(class_table(sm)), "n_windows_per_floor": int(pred.shape[0] * NW),
                   "median_az_std_by_floor": [float(np.median(f["az_std"][:, k])) for k in range(NF)]}
        keep[name] = (f, pred, sm)
    # impact: impacted floor separately from the rest
    f, pred, sm, imp = scenario(tree, seeds, "impact")
    imp = np.array(imp)
    onfloor = np.zeros(pred.shape, dtype=bool)
    onfloor[np.arange(len(imp)), imp - 1, :] = True
    imp_res = {"impacted_floor_all_floors_pooled": {}, "impacted_floor_by_floor": {}, "other_floors_by_floor": {}}
    for nm, arr in (("raw", pred), ("smoothed_2of3", sm)):
        pooled = np.bincount(arr[onfloor], minlength=3) / onfloor.sum()
        imp_res["impacted_floor_all_floors_pooled"][nm] = {c: float(pooled[i]) for i, c in enumerate(CLASSES)}
        imp_res["impacted_floor_by_floor"][nm] = {f"floor{k + 1}": {c: float(np.mean(arr[imp == k + 1, k, :] == i)) for i, c in enumerate(CLASSES)} for k in range(NF)}
        imp_res["other_floors_by_floor"][nm] = tables(class_table(arr, ~onfloor))
    imp_res["n_buildings"] = len(imp)
    R["impact_one_floor"] = imp_res
    # P1
    nz = keep["noise_only"][1]
    p1_frac = float((nz == 2).mean())
    nzf = keep["noise_only"][0]
    bins_le = int(np.floor(tree.h_thr["az_domfreq"] / BIN_HZ))
    P1 = {"statement": "noise-only windows are classified faulty about 70% of the time (az_std<=354, az_domfreq>13.67 Hz)",
          "faulty_fraction": p1_frac, "class_fractions": {c: float((nz == i).mean()) for i, c in enumerate(CLASSES)},
          "fraction_az_std_le_threshold": float((nzf["az_std"] <= tree.thr_az_std).mean()),
          "fraction_az_domfreq_gt_13p67": float((nzf["az_domfreq"] > float(tree.h_thr["az_domfreq"])).mean()),
          "analytic_expectation_for_white_noise": f"argmax uniform over 64 bins; bins 1..{bins_le} are <=13.67 Hz so faulty = 1-{bins_le}/64 = {1 - bins_le / 64:.4f}",
          "rule": "CONFIRMED if |faulty fraction - 0.70| <= 0.05 (decided before running)"}
    P1["verdict"] = "CONFIRMED" if abs(p1_frac - 0.70) <= 0.05 else "REFUTED"
    # P2
    scan = {}
    for m in ("healthy", "strong_2x", "strong_3x", "strong_5x"):
        f, pred, sm = keep[m]
        fr = (pred == 2).mean(axis=(0, 2))
        big = f["az_std"] > tree.thr_az_std
        scan[m] = {"faulty_fraction_by_floor": fr.tolist(), "frac_windows_az_std_over_354_by_floor": big.mean(axis=(0, 2)).tolist() if big.ndim == 3 else big.mean(axis=0).tolist(),
                   "abnormal_fraction_by_floor": (pred == 1).mean(axis=(0, 2)).tolist()}
    rho = {}
    for m in ("strong_3x", "strong_5x"):
        fr = np.array(scan[m]["faulty_fraction_by_floor"])
        rk = lambda v: np.argsort(np.argsort(v)).astype(float)
        rho[m] = {"spearman_floor_vs_faulty": float(np.corrcoef(rk(np.arange(NF)), rk(fr))[0, 1]), "top3_minus_bottom3": float(fr[-3:].mean() - fr[:3].mean())}
    f5, p5, _ = keep["strong_5x"]
    big = f5["az_std"] > tree.thr_az_std
    kb = big & (f5["az_kurt"] > tree.thr_kurt)
    P2 = {"statement": "in strong building-wide events the top floors report faulty first (az_std>354 with az_kurt>-1.19 -> faulty)",
          "scan_by_multiplier": scan, "trend": rho,
          "at_5x_of_windows_az_std_over_354_share_with_kurt_over_threshold": float(kb.sum() / max(big.sum(), 1)),
          "at_5x_of_windows_az_std_over_354_share_classified_faulty": float((p5[big] == 2).mean()) if big.any() else None,
          "at_5x_of_windows_az_std_over_354_share_abnormal": float((p5[big] == 1).mean()) if big.any() else None,
          "rule": "CONFIRMED if at 3x and 5x both Spearman>=0.7 and top3-bottom3 faulty fraction>=0.10; REFUTED if either top3-bottom3 <=0; else PARTIAL"}
    ok = all(rho[m]["spearman_floor_vs_faulty"] >= 0.7 and rho[m]["top3_minus_bottom3"] >= 0.10 for m in rho)
    bad = any(rho[m]["top3_minus_bottom3"] <= 0 for m in rho)
    P2["verdict"] = "CONFIRMED" if ok else ("REFUTED" if bad else "PARTIAL")
    return R, P1, P2, keep["healthy"][0]


# ------------------------------------------------------------------ Part C
def part_c(tree, ds, healthy_feats):
    az_std = ds["az_std"].to_numpy()
    per_class = {c: {"min": float(az_std[ds["label"] == c].min()), "p01": float(np.percentile(az_std[ds["label"] == c], 1))} for c in CLASSES}
    ds_min = float(az_std.min())
    # noise-only at the highest generator sigma (60 counts), int16 rounding included, 2,000,000 windows
    rng = np.random.default_rng(777)
    mx, allq = 0.0, []
    for _ in range(40):
        w = np.rint(AZ_B + rng.standard_normal((50_000, N)) * 60.0)
        s = w.std(axis=1)
        mx = max(mx, float(s.max()))
        allq.append(np.quantile(s, [0.999, 0.99999]))
    # chi bound: P(std > x) for 128 samples; exact upper tail via chi-square of 127 dof
    from scipy.stats import chi2
    n_w = 2_000_000
    tail_1e9 = float(60 * np.sqrt(chi2.isf(1e-9, N - 1) / N))
    noise_max = max(mx, 0.0)
    res = {"dataset_az_std_min_overall": ds_min, "dataset_az_std_by_class": per_class, "windows_in_dataset": int(len(az_std)),
           "noise_only_sigma60_windows_simulated": n_w, "noise_only_sigma60_max_az_std": noise_max,
           "noise_only_sigma60_p99.9": float(np.mean([q[0] for q in allq])), "noise_only_sigma60_p99.999": float(np.mean([q[1] for q in allq])),
           "noise_only_sigma60_analytic_tail_prob_1e-9": tail_1e9,
           "lower_bound_used_for_G": max(noise_max, tail_1e9)}
    lo = res["lower_bound_used_for_G"]
    res["G_exists"] = bool(lo < ds_min)
    if res["G_exists"]:
        G = 0.5 * (lo + ds_min)
        res["recommended_G"] = float(G)
        res["margin_above_noise_tail_counts"] = float(G - lo)
        res["margin_below_dataset_min_counts"] = float(ds_min - G)
        res["margin_note"] = "G placed at midpoint of [noise upper bound, dataset min]"
        under = (healthy_feats["az_std"] < G)  # (B,10,NW)
        res["healthy_simulated_floor_windows_under_G_by_floor"] = {f"floor{k + 1}": float(under[:, k].mean()) for k in range(NF)}
        res["healthy_simulated_floor_windows_under_G_overall"] = float(under.mean())
        res["note_fraction"] = "'under G' = az_std < G, i.e. window would be gated out as 'no vibration signal'; healthy default excitation, same windows as Part B healthy scenario"
    else:
        res["recommended_G"] = None
        res["statement"] = "No G exists: the noise-only upper tail reaches or exceeds the smallest dataset az_std."
    return res


AZ_B = float(bs.AZ_BASELINE)


# ------------------------------------------------------------------ exploratory (cannot affect the verdict)
def exploratory(thr_default):
    out = {"note": "exploratory only; disjoint seeds; does not affect the GATE verdict. 2 alternatives tried.", "n_tried": 2}
    # E1: analytic ceiling, no sampling error, no sensor noise (what localisation is possible in principle)
    seeds = seeds_block("expl1", 10 * 100)
    res = {}
    for lev in (0.30, 0.15):
        top, w1 = [], []
        for s in range(1, 11):
            for sd in seeds[(s - 1) * 100:s * 100]:
                p = bs.building_params(sd)
                a0, a1 = bs.expected_floor_std(p), bs.expected_floor_std(p, (s, lev))
                r = lambda a: np.concatenate([[a[0] / np.median(a)], a[1:] / a[:-1]])
                pred = int(np.argmax(np.abs(np.log(r(a1) / r(a0)))) + 1)
                top.append(pred == s)
                w1.append(abs(pred - s) <= 1)
        res[f"{int(lev * 100)}%"] = {"top1": float(np.mean(top)), "within1": float(np.mean(w1))}
    out["E1_analytic_ceiling_expected_amplitudes_no_noise_infinite_averaging"] = res
    # E2: same method, 120-window assessments (4x longer), 30 buildings per storey at 30%
    names = BASE["expl2"]
    seeds2 = list(range(names, names + 300))
    cs = list(range(names + 1000, names + 1300))
    DIc = assess(cs, [None] * 300, "default", n_windows=120)
    thr = float(np.percentile(np.abs(DIc).max(axis=1), FA_ALPHA_PCT))
    true = np.repeat(np.arange(1, 11), 30)
    DI = assess(seeds2, [(int(s), 0.30) for s in true], "default", n_windows=120)
    sc = score(DI, true, thr)
    out["E2_same_method_120_windows_30pct"] = {"threshold": thr, "n_per_storey": 30, "top1": sc["top1"], "within1": sc["within1"], "detect": sc["detect"],
                                              "top1_by_storey": {s: sc["per_storey"][s]["top1"] for s in range(1, 11)}}
    return out


# ------------------------------------------------------------------ main
def main():
    all_blocks = [(BASE[k], BASE[k] + {"calib": N_CALIB, "fa": N_FA, "score30": 1000, "score15": 1000, "b_calib": N_B_CALIB, "b_fa": N_B_FA,
                                       "b_score30": 1000, "b_score15": 1000, "partB": N_PARTB, "noise": 1, "expl1": 1000, "expl2": 1300}[k]) for k in BASE]
    rngs = sorted(all_blocks)
    assert all(rngs[i][1] <= rngs[i + 1][0] for i in range(len(rngs) - 1)), "seed blocks overlap"

    log("== sanity checks (building_sim) ==")
    san = bs.sanity_checks(verbose=False)
    log("  passed:", {k: v for k, v in san.items() if k in ("f1_eig_max_rel_err", "damage_lowers_f1", "deterministic_and_batch_independent", "dlpf_gain_measured")})

    tree = Tree()
    log("== tree check ==")
    ps = [bs.building_params(s) for s in seeds_block("noise", 1)] + [bs.building_params(s) for s in range(77_000, 77_020)]
    sig = bs.simulate_batch(ps[1:], 1, 5, [(3, 0.3)] * 20, "default", 4.0)
    az, gx = sensor_windows(ps[1:], sig, 1, [(i % 10) + 1 for i in range(20)])
    chk, ds = tree_check(tree, feats5(az, gx))
    log(" ", chk)

    log("== Part A (default excitation) ==")
    names_d = {"calib": "calib", "fa": "fa", "s30": "score30", "s15": "score15"}
    A = run_localisation("default", "default", N_CALIB, N_FA, names_d)
    log("== Part A sensitivity (broadband 2-45 Hz) ==")
    names_b = {"calib": "b_calib", "fa": "b_fa", "s30": "b_score30", "s15": "b_score15"}
    A_b = run_localisation("broadband", "broadband", N_B_CALIB, N_B_FA, names_b)

    s30 = A["levels"]["30%"]
    crit = {"top1_ge_0.70": s30["top1"] >= PASS_TOP1, "within1_ge_0.90": s30["within1"] >= PASS_WITHIN1, "false_alarm_le_0.05": A["false_alarm_rate"] <= PASS_FA}
    passed = all(crit.values())

    log("== Part B ==")
    B, P1, P2, healthy_f = part_b(tree)
    log(f"  P1 {P1['verdict']} (faulty {P1['faulty_fraction']:.3f});  P2 {P2['verdict']}  t={time.time() - T0:.0f}s")
    log("== Part C ==")
    C = part_c(tree, ds, healthy_f)
    log("== exploratory ==")
    E = exploratory(A["threshold_p95_max_abs_DI"])

    meta = {
        "task": "T1 localisation gate", "runtime_s": round(time.time() - T0, 1),
        "excitation_scale_reasoning": bs.excitation_reasoning(),
        "gx_note": "gx is MODELLED, NOT PHYSICAL (as in generate.py): gain U(0.02,0.2) x floor lateral signal + noise sigma U(5,20) + constant offset N(0,15). Only gx_std and gx_domfreq reach the tree.",
        "az_note": "az = lateral (sway) absolute floor acceleration; counts 16384/g; DLPF 2nd-order Butterworth 44 Hz at 1 kHz (CFG=3 emulation, copied from dlpf_check.py) then decimated to 100 Hz; sensor noise per node sigma U(20,60) added after decimation.",
        "method_fixed_in_advance": {
            "windows_per_assessment": NW, "noise_estimate": "separate 30-window noise-only commissioning recording per building: noise_var_k = mean over windows of az_std^2",
            "s_k": f"sqrt(max(mean(az_std_k^2) - noise_var_k, {EPS}))", "ratio": "r_k = s_k/s_(k-1), k=2..10; r_1 = s_1/median(s_1..s_10) (no sensor below storey 1)",
            "baseline": "r0 from a healthy recording of the same building, different excitation seed (exc_id 0 vs 1)", "DI": "DI_k = log(r_k/r0_k); predicted storey = argmax|DI_k|; alarm if max|DI_k| > threshold",
            "threshold": f"{FA_ALPHA_PCT}th percentile of max|DI| over {N_CALIB} healthy calibration buildings (seeds disjoint from scoring); broadband variant recalibrated on {N_B_CALIB} broadband healthy buildings",
            "scoring": f"{N_PER_CELL} fresh buildings per storey per damage level (different building seeds per cell); false alarm on {N_FA} fresh healthy assessments",
            "top1_definition": "unconditional (all damaged buildings regardless of alarm); conditional-on-alarm also reported",
            "pass_criterion": "30% loss, default excitation: top-1 >= 0.70 AND within-one >= 0.90 AND false alarm <= 0.05"},
        "seed_blocks": BASE, "burn_in_note": "6.4 s of 1 kHz signal discarded. The structural response is computed as the stationary (periodic) frequency-domain solution so it has no structural transient; the burn-in absorbs the DLPF start-up transient.",
        "excitation_bands_hz": bs.BANDS, "damping": "single modal damping ratio per building (U 1-3%) applied to all modes",
        "impact_assumption": "impact f0 per recording drawn from generator ranges 4-12 Hz or 14-30 Hz (50/50); spikes added after decimation to az and gain*spikes to gx of one floor; rate U(0.8,2.0)/s",
        "strong_excitation_definition": "3x/5x = base-acceleration amplitude multiplier applied to every building",
    }
    result = {"meta": meta, "sanity_checks_building_sim": san, "tree_check": chk,
              "part_A": {"default": A, "broadband_sensitivity": A_b,
                         "criterion_results": crit, "criterion_values": {"top1_30": s30["top1"], "within1_30": s30["within1"], "false_alarm": A["false_alarm_rate"]}},
              "part_B": {"scenarios": B, "P1": P1, "P2": P2}, "part_C": C, "exploratory": E, "gate": "PASS" if passed else "FAIL"}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=1, default=lambda o: o.tolist() if isinstance(o, np.ndarray) else o.item() if isinstance(o, np.generic) else str(o)))
    summary(result)
    return passed


def summary(r):
    A, Ab = r["part_A"]["default"], r["part_A"]["broadband_sensitivity"]
    log("\n================ SUMMARY ================")
    for nm, a in (("DEFAULT excitation (2-12 Hz)", A), ("BROADBAND sensitivity (2-45 Hz)", Ab)):
        log(f"\n{nm}: threshold={a['threshold_p95_max_abs_DI']:.4f}  false alarm={a['false_alarm_rate']:.4f} ({a['n_healthy_assessments']} healthy)")
        for lev in ("30%", "15%"):
            s = a["levels"][lev]
            log(f"  {lev} loss overall: top1={s['top1']:.3f} within1={s['within1']:.3f} detect={s['detect']:.3f} (n={s['n']})")
            log("    storey:  " + " ".join(f"{k:>5}" for k in range(1, 11)))
            for key in ("top1", "within1", "detect"):
                log(f"    {key:8s} " + " ".join(f"{s['per_storey'][k][key]:5.2f}" for k in range(1, 11)))
        for lev, f in a["f1_shift"].items():
            log(f"  f1 shift @{lev}: mean |shift|={f['mean_abs_shift_hz_overall']:.3f} Hz ({f['mean_abs_shift_over_bin']:.2f} bins), max {f['max_abs_shift_hz']:.3f} Hz, "
                f"{100 * f['frac_buildings_abs_shift_over_one_bin']:.1f}% of buildings shift > 1 bin ({BIN_HZ} Hz)")
    log("\nPart B: P1 " + r["part_B"]["P1"]["verdict"] + f" (noise-only faulty fraction {r['part_B']['P1']['faulty_fraction']:.3f})")
    log("Part B: P2 " + r["part_B"]["P2"]["verdict"] + " " + json.dumps(r["part_B"]["P2"]["trend"]))
    C = r["part_C"]
    log(f"\nPart C: dataset az_std min {C['dataset_az_std_min_overall']:.2f} ({ {k: round(v['min'], 1) for k, v in C['dataset_az_std_by_class'].items()} }); noise-only max {C['noise_only_sigma60_max_az_std']:.2f}; "
        f"G exists: {C['G_exists']}; recommended G: {C.get('recommended_G')}")
    if C["G_exists"]:
        log("  healthy floor windows under G: " + ", ".join(f"f{k + 1}={C['healthy_simulated_floor_windows_under_G_by_floor'][f'floor{k + 1}']:.2f}" for k in range(NF)))
    c = r["part_A"]["criterion_values"]
    log(f"\nCriterion (30%, default): top1 {c['top1_30']:.3f} (>=0.70), within1 {c['within1_30']:.3f} (>=0.90), false alarm {c['false_alarm']:.4f} (<=0.05)")
    log(f"runtime {r['meta']['runtime_s']} s; wrote {OUT}")
    log("GATE: " + r["gate"])


if __name__ == "__main__":
    main()
