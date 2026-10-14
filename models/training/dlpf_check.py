import json
import time
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeClassifier

T0 = time.time()
SEED = 42
FS = 100
N = 128
UP = 10
FS_UP = FS * UP
PAD = 640
NW = 40
CLASSES = ["normal", "abnormal", "faulty"]
AXES = ["ax", "ay", "az", "gx", "gy", "gz"]
FEATS = ["std", "kurt", "domfreq"]
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "models" / "selected" / "validation"
VARIANTS = {
    "A": {"desc": "2nd-order Butterworth LP, 44 Hz accel / 42 Hz gyro (CFG=3 emulation)", "order": 2, "fc": [44, 44, 44, 42, 42, 42]},
    "B": {"desc": "1st-order LP, 44 Hz accel / 42 Hz gyro", "order": 1, "fc": [44, 44, 44, 42, 42, 42]},
    "C": {"desc": "2nd-order Butterworth LP, 21 Hz accel / 20 Hz gyro (CFG=4 stress case)", "order": 2, "fc": [21, 21, 21, 20, 20, 20]},
}

df = pd.read_csv(ROOT / "data" / "processed" / "dataset.csv")
spec = json.loads((ROOT / "models" / "selected" / "feature_spec.json").read_text())
cols18 = spec["input_columns"]
used = spec["used_feature_columns"]
assert cols18 == [f"{a}_{f}" for a in AXES for f in FEATS]
raw_cols = [f"{a}_{i}" for a in AXES for i in range(N)]
y = df["label"].map({c: i for i, c in enumerate(CLASSES)}).to_numpy()
rec = df["recording_id"].to_numpy()
windows = np.ascontiguousarray(df[raw_cols].to_numpy(dtype=np.float64)).reshape(-1, 6, N)
n_rec = int(rec.max()) + 1
assert len(df) == n_rec * NW == 6000 and n_rec == 150
assert (rec.reshape(n_rec, NW) == np.arange(n_rec)[:, None]).all()
assert (y.reshape(n_rec, NW) == y.reshape(n_rec, NW)[:, :1]).all()
rec_label = y.reshape(n_rec, NW)[:, 0]
model = joblib.load(ROOT / "models" / "selected" / "model.joblib")
assert model.max_depth == 3 and list(model.classes_) == [0, 1, 2]
tree = model.tree_


def feats18(w):
    x = w.astype(np.float64)
    xc = x - x.mean(axis=2, keepdims=True)
    var = (xc ** 2).mean(axis=2)
    std = np.sqrt(var)
    kurt = np.where(var > 0, (xc ** 4).mean(axis=2) / np.where(var > 0, var, 1) ** 2 - 3, 0.0)
    mag = np.abs(np.fft.rfft(xc, axis=2))
    dom = (np.argmax(mag[:, :, 1:], axis=2) + 1) * FS / N
    per = {"std": std, "kurt": kurt, "domfreq": dom}
    return np.stack([per[f][:, ai] for ai in range(6) for f in FEATS], axis=1)


Xu = feats18(windows)
assert np.allclose(Xu, df[cols18].to_numpy(dtype=np.float64), rtol=1e-9, atol=1e-9)
assert np.array_equal(Xu[:, [i for i, c in enumerate(cols18) if c.endswith("domfreq")]], df[[c for c in cols18 if c.endswith("domfreq")]].to_numpy())
print("feature functions reproduce CSV 18 features (domfreq exactly)")
pu = model.predict(Xu)
print("deployed model unfiltered in-sample accuracy:", round(float((pu == y).mean()), 4))

rec_sig = windows.reshape(n_rec, NW, 6, N).transpose(0, 2, 1, 3).reshape(n_rec, 6, NW * N)
L = NW * N
sig = rec_sig.reshape(n_rec * 6, L)
assert np.array_equal(sig.reshape(n_rec, 6, L), rec_sig)

left = 2 * sig[:, :1] - sig[:, PAD:0:-1]
right = 2 * sig[:, -1:] - sig[:, -2:-PAD - 2:-1]
padded = np.concatenate([left, sig, right], axis=1)
M = padded.shape[1]
assert M == L + 2 * PAD and M % 2 == 0
spec_in = np.fft.rfft(padded, axis=1)
spec_out = np.zeros((padded.shape[0], UP * M // 2 + 1), dtype=complex)
spec_out[:, :M // 2] = spec_in[:, :M // 2]
spec_out[:, M // 2] = spec_in[:, M // 2] / 2
up = np.fft.irfft(spec_out, n=UP * M, axis=1) * UP
up = np.ascontiguousarray(up[:, UP * PAD:UP * PAD + UP * L])
del padded, spec_in, spec_out, left, right
assert up.shape == (n_rec * 6, UP * L)

rt = up[:, ::UP]
rt_err = float(np.abs(rt - sig).max())
rt_int = np.clip(np.rint(rt), -32768, 32767)
rt_int_err = float(np.abs(rt_int - sig).max())
assert rt_int_err == 0
rt_w = np.ascontiguousarray(rt_int.reshape(n_rec, 6, NW, N).transpose(0, 2, 1, 3)).reshape(-1, 6, N)
assert np.array_equal(rt_w, windows)
assert np.array_equal(feats18(rt_w), Xu) and np.array_equal(model.predict(feats18(rt_w)), pu)
print(f"upsample round trip: max abs error before rounding {rt_err:.3e}; after rounding {rt_int_err:.0f}; windows, features and predictions identical")


def design(order, fc, fs):
    k = np.tan(np.pi * np.asarray(fc, dtype=np.float64) / fs)
    if order == 2:
        n = 1 / (1 + np.sqrt(2) * k + k ** 2)
        b = np.stack([k ** 2 * n, 2 * k ** 2 * n, k ** 2 * n])
        a = np.stack([2 * (k ** 2 - 1) * n, (1 - np.sqrt(2) * k + k ** 2) * n])
    else:
        b = np.stack([k / (1 + k), k / (1 + k), np.zeros_like(k)])
        a = np.stack([(k - 1) / (1 + k), np.zeros_like(k)])
    return b, a


def response(b, a, f, fs):
    z = np.exp(-1j * 2 * np.pi * np.asarray(f, dtype=np.float64) / fs)
    return (b[0] + b[1] * z + b[2] * z ** 2) / (1 + a[0] * z + a[1] * z ** 2)


def group_delay_ms(b, a, fs):
    f = np.array([0.5, 1.5])
    ph = np.angle(response(b, a, f, fs))
    return float(-(ph[1] - ph[0]) / (2 * np.pi * (f[1] - f[0])) * 1000)


def lfilter_ss(x, b, a, step):
    b0, b1, b2 = b
    a1, a2 = a
    x0 = x[:, 0]
    y0 = x0 * (b0 + b1 + b2) / (1 + a1 + a2)
    z2 = b2 * x0 - a2 * y0
    z1 = b1 * x0 - a1 * y0 + z2
    xt = np.ascontiguousarray(x.T)
    out = np.empty((x.shape[0], x.shape[1] // step))
    for i in range(xt.shape[0]):
        xi = xt[i]
        yi = b0 * xi + z1
        z1 = b1 * xi - a1 * yi + z2
        z2 = b2 * xi - a2 * yi
        if i % step == 0:
            out[:, i // step] = yi
        if i == 0:
            assert np.allclose(yi, y0, rtol=1e-9, atol=1e-6)
    return out


def cut(dec):
    q = np.clip(np.rint(dec), -32768, 32767)
    return np.ascontiguousarray(q.reshape(n_rec, 6, NW, N).transpose(0, 2, 1, 3)).reshape(-1, 6, N)


def conv(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError


names_of = [None if f < 0 else cols18[f] for f in tree.feature]

results = {
    "meta": {
        "method": "FFT x10 upsample (odd reflect pad 640 each side, Nyquist bin split, crop) -> causal IIR at 1 kHz with steady-state initial conditions at sample 0 -> take every 10th sample (aligned to original sample times) -> round/clip int16 -> same 40x128 windows -> same feature definitions",
        "fs_hz": FS, "fs_upsampled_hz": FS_UP, "n_windows": int(len(y)), "n_recordings": int(n_rec),
        "deployed_model_in_sample_note": "the deployed model was trained on all 150 recordings, so its accuracy on this dataset is in-sample and optimistic; flip counts are the primary metric",
        "limitation": "the upsampled signal is band-limited to <50 Hz (original Nyquist); the real sensor sees wideband input at 1 kHz before filtering, so aliasing of >50 Hz content into the 100 Hz samples is not reproduced",
        "roundtrip": {"max_abs_error_before_rounding": rt_err, "max_abs_error_after_rounding": rt_int_err, "windows_identical": True, "features_identical": True, "predictions_identical": True},
        "tree_nodes": {int(n): {"feature": names_of[n], "threshold": float(tree.threshold[n])} for n in range(tree.node_count) if tree.feature[n] >= 0},
        "unfiltered_in_sample_accuracy": float((pu == y).mean()),
    },
    "variants": {},
}

filtered = {}
for vk, v in VARIANTS.items():
    fc6 = np.array(v["fc"], dtype=np.float64)
    b6, a6 = design(v["order"], fc6, FS_UP)
    fcs = np.tile(fc6, n_rec)
    b, a = design(v["order"], fcs, FS_UP)
    t1 = time.time()
    dec = lfilter_ss(up, b, a, UP)
    filtered[vk] = feats18(cut(dec))
    fr = [5, 10, 20, 30, 40, 50]
    results["variants"][vk] = {
        "description": v["desc"], "order": v["order"], "fc_hz_by_axis": dict(zip(AXES, v["fc"])),
        "coefficients_by_axis": {ax: {"b": b6[:, i], "a": [1.0, a6[0, i], a6[1, i]]} for i, ax in enumerate(AXES)},
        "group_delay_ms_at_low_freq": {ax: group_delay_ms(b6[:, i], a6[:, i], FS_UP) for i, ax in enumerate(AXES)},
        "gain_at_fc": {ax: float(np.abs(response(b6[:, i], a6[:, i], v["fc"][i], FS_UP))) for i, ax in enumerate(AXES)},
        "gain_at_hz": {"freqs": fr, "accel_axis": np.abs(response(b6[:, 0], a6[:, 0], fr, FS_UP)), "gyro_axis": np.abs(response(b6[:, 3], a6[:, 3], fr, FS_UP))},
    }
    print(f"variant {vk} filtered in {time.time() - t1:.1f} s")
del up

for vk in VARIANTS:
    Xf = filtered[vk]
    pf = model.predict(Xf)
    flip = pf != pu
    fm = np.zeros((3, 3), dtype=int)
    for s, t in zip(pu[flip], pf[flip]):
        fm[s, t] += 1
    by_label = {CLASSES[c]: {"windows": int((y == c).sum()), "flips": int(flip[y == c].sum()), "flip_rate": float(flip[y == c].mean())} for c in range(3)}
    path_u = model.decision_path(Xu).toarray().astype(bool)
    path_f = model.decision_path(Xf).toarray().astype(bool)
    causes = {}
    for i in np.where(flip)[0]:
        node = 0
        while tree.children_left[node] != -1:
            f = tree.feature[node]
            cu = tree.children_left[node] if np.float32(Xu[i, f]) <= tree.threshold[node] else tree.children_right[node]
            cf = tree.children_left[node] if np.float32(Xf[i, f]) <= tree.threshold[node] else tree.children_right[node]
            if cu != cf:
                break
            node = cu
        key = f"node{node}: {names_of[node]} <= {tree.threshold[node]:.6g}"
        causes[key] = causes.get(key, 0) + 1
    shifts = {}
    for c in range(3):
        m = y == c
        d = {}
        for name in used:
            j = cols18.index(name)
            xu, xf = Xu[m, j], Xf[m, j]
            if name.endswith("_std"):
                r = np.where(xu > 0, xf / np.where(xu > 0, xu, 1), np.nan)
                d[name] = {"median_ratio_filt_over_unfilt": float(np.nanmedian(r)), "p10_ratio": float(np.nanpercentile(r, 10)), "p90_ratio": float(np.nanpercentile(r, 90))}
            elif name.endswith("_kurt"):
                d[name] = {"median_change": float(np.median(xf - xu)), "median_unfilt": float(np.median(xu)), "median_filt": float(np.median(xf))}
            else:
                d[name] = {"fraction_changed": float((xf != xu).mean()), "median_change_hz": float(np.median(xf - xu)), "mean_abs_change_hz": float(np.abs(xf - xu).mean())}
        shifts[CLASSES[c]] = d
    results["variants"][vk]["deployed_model"] = {
        "n_flips": int(flip.sum()), "flip_pct": float(100 * flip.mean()),
        "flip_matrix_from_row_to_col": {"classes": CLASSES, "counts": fm},
        "flips_by_true_label": by_label,
        "in_sample_accuracy_unfiltered": float((pu == y).mean()), "in_sample_accuracy_filtered": float((pf == y).mean()),
        "flip_cause_first_changed_branch": dict(sorted(causes.items(), key=lambda kv: -kv[1])),
    }
    results["variants"][vk]["feature_shifts_by_class"] = shifts
    results["variants"][vk]["path_changed_windows"] = int((path_u != path_f).any(axis=1).sum())

n_rep = 10
cv = {vk: {"acc": np.zeros((n_rep, 5)), "flip": np.zeros((n_rep, 5))} for vk in VARIANTS}
acc_u = np.zeros((n_rep, 5))
ids_by_class = {c: np.where(rec_label == c)[0] for c in range(3)}
for r in range(n_rep):
    rr = np.random.default_rng(SEED + 7000 + r)
    fids = [[] for _ in range(5)]
    for c in range(3):
        perm = rr.permutation(ids_by_class[c])
        for k in range(5):
            fids[k] += list(perm[k * 10:(k + 1) * 10])
    assert sorted(i for f in fids for i in f) == list(range(n_rec))
    for k in range(5):
        te = np.isin(rec, fids[k])
        assert te.sum() == 1200 and all((te & (y == c)).sum() == 400 for c in range(3))
        m = DecisionTreeClassifier(max_depth=3, random_state=42).fit(Xu[~te], y[~te])
        p0 = m.predict(Xu[te])
        acc_u[r, k] = (p0 == y[te]).mean()
        for vk in VARIANTS:
            p1 = m.predict(filtered[vk][te])
            cv[vk]["acc"][r, k] = (p1 == y[te]).mean()
            cv[vk]["flip"][r, k] = (p1 != p0).mean()

for vk in VARIANTS:
    ru = acc_u.mean(axis=1)
    rf = cv[vk]["acc"].mean(axis=1)
    dr = rf - ru
    df_ = (cv[vk]["acc"] - acc_u).ravel()
    results["variants"][vk]["cv"] = {
        "scheme": "grouped stratified 5-fold over 150 recordings, 10 recordings/class per test fold, 10 repeats, seeds 42+7000+r; dt depth 3 trained on UNFILTERED train folds",
        "mean_accuracy_unfiltered": float(ru.mean()), "mean_accuracy_filtered": float(rf.mean()),
        "paired_mean_diff_filtered_minus_unfiltered": float(dr.mean()),
        "se_over_10_repeat_means": float(dr.std(ddof=1) / np.sqrt(n_rep)),
        "se_over_50_folds_note": "folds and repeats reuse the same recordings, so both SEs understate true uncertainty",
        "se_over_50_folds": float(df_.std(ddof=1) / np.sqrt(len(df_))),
        "min_repeat_diff": float(dr.min()), "max_repeat_diff": float(dr.max()),
        "mean_flip_rate_test_folds": float(cv[vk]["flip"].mean()),
        "repeat_means_unfiltered": ru, "repeat_means_filtered": rf,
    }

results["meta"]["runtime_s"] = round(time.time() - T0, 1)
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "dlpf_check.json").write_text(json.dumps(results, indent=2, default=conv))

print("\nSUMMARY")
for vk, v in results["variants"].items():
    d, c = v["deployed_model"], v["cv"]
    print(f"\nVariant {vk}: {v['description']}")
    print("  group delay (ms): " + ", ".join(f"{ax} {gd:.3f}" for ax, gd in v["group_delay_ms_at_low_freq"].items()))
    print("  coeff accel b", np.round(v["coefficients_by_axis"]["az"]["b"], 9).tolist(), "a", np.round(v["coefficients_by_axis"]["az"]["a"], 9).tolist())
    print("  coeff gyro  b", np.round(v["coefficients_by_axis"]["gx"]["b"], 9).tolist(), "a", np.round(v["coefficients_by_axis"]["gx"]["a"], 9).tolist())
    print(f"  deployed flips: {d['n_flips']} / 6000 ({d['flip_pct']:.2f}%)")
    print("  flip matrix (row from, col to):", d["flip_matrix_from_row_to_col"]["counts"].tolist())
    print("  flips by true label:", {k: (x['flips'], round(x['flip_rate'], 4)) for k, x in d["flips_by_true_label"].items()})
    print(f"  in-sample acc (optimistic) unfiltered {d['in_sample_accuracy_unfiltered']:.4f} -> filtered {d['in_sample_accuracy_filtered']:.4f}")
    print("  flip causes:", d["flip_cause_first_changed_branch"])
    print(f"  CV acc unfiltered {c['mean_accuracy_unfiltered']:.4f} filtered {c['mean_accuracy_filtered']:.4f} diff {c['paired_mean_diff_filtered_minus_unfiltered']:+.4f} SE(rep) {c['se_over_10_repeat_means']:.4f} SE(folds) {c['se_over_50_folds']:.4f} flip rate {c['mean_flip_rate_test_folds']:.4f}")
    for cl, sh in v["feature_shifts_by_class"].items():
        print(f"  shifts [{cl}] az_std ratio {sh['az_std']['median_ratio_filt_over_unfilt']:.3f}, gx_std ratio {sh['gx_std']['median_ratio_filt_over_unfilt']:.3f}, az_kurt change {sh['az_kurt']['median_change']:+.3f}, az_domfreq changed {sh['az_domfreq']['fraction_changed']:.3f}, gx_domfreq changed {sh['gx_domfreq']['fraction_changed']:.3f}")
print(f"\nruntime {time.time() - T0:.1f} s; wrote {OUT / 'dlpf_check.json'}")
