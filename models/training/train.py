import json
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

SEED = 42
FS = 100
N = 128
CLASSES = ["normal", "abnormal", "faulty"]
AXES = ["ax", "ay", "az", "gx", "gy", "gz"]
FEATS = ["rms", "std", "kurt", "domfreq"]
THRESH = 0.90
MAX_BYTES = 5120
REPEATS = 10
N_REDRAW = 200
N_CV_FULL = 40
N_OFFSET_DRAWS = 10
OFFSET_RANGE = 1000
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "models" / "selected"
OUT.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(ROOT / "data" / "processed" / "dataset.csv")
feat24 = [f"{a}_{f}" for a in AXES for f in FEATS]
raw_cols = [f"{a}_{i}" for a in AXES for i in range(N)]
assert list(df.columns[-24:]) == feat24
y = df["label"].map({c: i for i, c in enumerate(CLASSES)}).to_numpy()
rec = df["recording_id"].to_numpy()
windows = df[raw_cols].to_numpy(dtype=np.float64).reshape(-1, 6, N)
X24_csv = df[feat24].to_numpy(dtype=np.float64)
band_names = [f"{a}_band_{b}" for a in AXES for b in ["low", "mid", "high"]]


def compute_feats(w, with_rms=False, with_band=False):
    x = w.astype(np.float64)
    xc = x - x.mean(axis=2, keepdims=True)
    var = (xc ** 2).mean(axis=2)
    mag = np.abs(np.fft.rfft(xc, axis=2))
    per = {
        "rms": np.sqrt((x ** 2).mean(axis=2)),
        "std": np.sqrt(var),
        "kurt": np.where(var > 0, (xc ** 4).mean(axis=2) / np.where(var > 0, var, 1) ** 2 - 3, 0.0),
        "domfreq": (np.argmax(mag[:, :, 1:], axis=2) + 1) * FS / N,
    }
    cols, names = [], []
    for ai, a in enumerate(AXES):
        for f in FEATS:
            if f == "rms" and not with_rms:
                continue
            cols.append(per[f][:, ai])
            names.append(f"{a}_{f}")
    if with_band:
        e = mag ** 2 / N
        fr = np.arange(N // 2 + 1) * FS / N
        for ai in range(6):
            for m in [(fr >= fr[1]) & (fr < 4), (fr >= 4) & (fr < 14), fr >= 14]:
                cols.append(e[:, ai, m].sum(axis=1))
        names += band_names
    return np.stack(cols, axis=1), names


Xchk, nchk = compute_feats(windows, with_rms=True)
assert nchk == feat24 and np.allclose(Xchk, X24_csv)
X18chk, n18chk = compute_feats(windows)
assert n18chk == [c for c in feat24 if not c.endswith("_rms")] and len(n18chk) == 18
assert np.allclose(X18chk, df[n18chk].to_numpy(dtype=np.float64))
print("numpy feature recomputation matches CSV (24 and 18 features)")

rng = np.random.default_rng(SEED)
rec_class = {int(r): int(c) for r, c in zip(rec, y)}
holdout_ids, dev_ids = {}, {}
for c in range(3):
    ids = np.array(sorted(r for r, k in rec_class.items() if k == c))
    ho = rng.choice(ids, 10, replace=False)
    dev = np.array(sorted(set(ids) - set(ho)))
    rng.permutation(dev)
    holdout_ids[c] = sorted(int(i) for i in ho)
    dev_ids[c] = sorted(int(i) for i in dev)

ho_set = {i for v in holdout_ids.values() for i in v}
dev_set = {i for v in dev_ids.values() for i in v}
assert len(ho_set) == 30 and len(dev_set) == 120 and not ho_set & dev_set
ho_mask = np.isin(rec, list(ho_set))
dev_mask = np.isin(rec, list(dev_set))
assert not (ho_mask & dev_mask).any() and (ho_mask | dev_mask).all()
rec_dev = rec[dev_mask]

fold_assign, fold_te = [], []
for r in range(REPEATS):
    rr = np.random.default_rng(SEED + 1 + r)
    fids = [dict() for _ in range(5)]
    for c in range(3):
        perm = rr.permutation(np.array(dev_ids[c]))
        for k in range(5):
            fids[k][c] = sorted(int(i) for i in perm[k * 8:(k + 1) * 8])
    sets = [{i for v in fids[k].values() for i in v} for k in range(5)]
    for a in range(5):
        for b in range(a + 1, 5):
            assert not sets[a] & sets[b]
    assert set().union(*sets) == dev_set and sum(len(s) for s in sets) == 120
    assert not ho_set & set().union(*sets)
    tes = []
    for k in range(5):
        assert all(len(fids[k][c]) == 8 and all(rec_class[i] == c for i in fids[k][c]) for c in range(3))
        te = np.isin(rec_dev, list(sets[k]))
        assert not set(rec_dev[~te]) & set(rec_dev[te])
        assert set(rec_dev[te]) == sets[k] and set(rec_dev[~te]) == dev_set - sets[k]
        tes.append(te)
    assert (sum(t.astype(int) for t in tes) == 1).all()
    fold_assign.append([{CLASSES[c]: fids[k][c] for c in range(3)} for k in range(5)])
    fold_te.append(tes)
print("split assertions passed for", REPEATS, "repeats")


def counts(mask):
    return {CLASSES[c]: {"recordings": int(len(set(rec[mask & (y == c)]))), "windows": int((mask & (y == c)).sum())} for c in range(3)}


split = {"dev": counts(dev_mask), "holdout": counts(ho_mask),
         "per_fold_train": {CLASSES[c]: {"recordings": 32, "windows": 1280} for c in range(3)},
         "per_fold_test": {CLASSES[c]: {"recordings": 8, "windows": 320} for c in range(3)}}
print("dev", split["dev"])
print("holdout", split["holdout"])

configs = []
for d in [3, 4, 5, 6, 7, 8]:
    configs.append({"name": f"dt_d{d}", "family": "tree", "params": {"max_depth": d}})
for n in [5, 10, 20, 50]:
    for d in [4, 6, 8]:
        configs.append({"name": f"rf_n{n}_d{d}", "family": "forest", "params": {"n_estimators": n, "max_depth": d}})
for h in [(8,), (16,), (32,), (16, 16)]:
    configs.append({"name": "mlp_" + "x".join(map(str, h)), "family": "mlp", "params": {"hidden_layer_sizes": list(h)}})
cfg_of = {c["name"]: c for c in configs}


def make(cfg):
    p = cfg["params"]
    if cfg["family"] == "tree":
        return DecisionTreeClassifier(max_depth=p["max_depth"], random_state=SEED)
    if cfg["family"] == "forest":
        return RandomForestClassifier(n_estimators=p["n_estimators"], max_depth=p["max_depth"], random_state=SEED, n_jobs=1)
    return make_pipeline(StandardScaler(), MLPClassifier(hidden_layer_sizes=tuple(p["hidden_layer_sizes"]), max_iter=2000, random_state=SEED))


def leaf_classes(t, est):
    assert list(est.classes_.astype(int)) == [0, 1, 2]
    return est.classes_.astype(int)[np.argmax(t.value[:, 0, :], axis=1)]


def rf_votes(model, X):
    v = np.zeros((len(X), 3), dtype=int)
    for e in model.estimators_:
        lc = leaf_classes(e.tree_, e)
        v[np.arange(len(X)), lc[e.apply(X.astype(np.float32))]] += 1
    return v


def predict(family, model, X):
    if family == "forest":
        return np.argmax(rf_votes(model, X), axis=1)
    return model.predict(X).astype(int)


def qpredict(pipe, X):
    mlp = pipe[1]
    a = pipe[0].transform(X)
    last = len(mlp.coefs_) - 1
    for i, (w, b) in enumerate(zip(mlp.coefs_, mlp.intercepts_)):
        s = np.abs(w).max() / 127
        a = a @ (np.round(w / s) * s) + b
        if i < last:
            a = np.maximum(a, 0)
    return mlp.classes_[np.argmax(a, axis=1)]


def dpredict(family, model, X):
    if family == "mlp":
        return qpredict(model, X).astype(int)
    return predict(family, model, X)


def footprint(cfg, model, n_in):
    if cfg["family"] == "mlp":
        mlp = model[1]
        assert mlp.activation == "relu"
        w = sum(c.size for c in mlp.coefs_)
        b = sum(i.size for i in mlp.intercepts_)
        layers = len(mlp.coefs_)
        return {
            "weights": int(w), "biases": int(b), "layers": layers,
            "int8_bytes": int(w + 4 * b + 4 * layers + 8 * n_in),
            "float32_bytes": int(4 * (w + b) + 8 * n_in),
            "deployed_bytes": int(w + 4 * b + 4 * layers + 8 * n_in),
            "macs": int(w),
        }
    trees = [model.tree_] if cfg["family"] == "tree" else [e.tree_ for e in model.estimators_]
    nodes = sum(t.node_count for t in trees)
    return {
        "nodes": int(nodes), "float32_bytes": int(10 * nodes), "int16_bytes": int(8 * nodes),
        "deployed_bytes": int(10 * nodes), "max_comparisons": int(sum(t.max_depth for t in trees)),
    }


def metrics(yt, yp):
    cm = np.zeros((3, 3), dtype=int)
    for t, p in zip(yt, yp):
        cm[t, p] += 1
    prec = [cm[c, c] / cm[:, c].sum() if cm[:, c].sum() else 0.0 for c in range(3)]
    rec_ = [cm[c, c] / cm[c].sum() if cm[c].sum() else 0.0 for c in range(3)]
    return {
        "accuracy": round(float((yt == yp).mean()), 6),
        "precision": {CLASSES[c]: round(float(prec[c]), 6) for c in range(3)},
        "recall": {CLASSES[c]: round(float(rec_[c]), 6) for c in range(3)},
        "confusion_matrix": cm.tolist(),
    }


def select(results):
    key = lambda r: (r["footprint"]["deployed_bytes"], -r["cv"]["mean"])
    best = lambda r: (-r["cv"]["mean"], r["footprint"]["deployed_bytes"])
    elig = [r for r in results if r["eligible"]]
    winner = min(elig, key=key)["name"] if elig else None
    reps = {}
    for fam in ["tree", "forest", "mlp"]:
        rs = [r for r in results if r["family"] == fam]
        ok = [r for r in rs if r["eligible"]]
        reps[fam] = min(ok, key=key)["name"] if ok else min(rs, key=best)["name"]
    return winner, reps


def run(with_band):
    X, names = compute_feats(windows, with_band=with_band)
    Xd, yd = X[dev_mask], y[dev_mask]
    results, oofs, models = [], {}, {}
    for cfg in configs:
        oof = np.zeros((REPEATS, len(yd)), dtype=int)
        accs = np.zeros((REPEATS, 5))
        qaccs = []
        for r in range(REPEATS):
            for k in range(5):
                te = fold_te[r][k]
                m = make(cfg).fit(Xd[~te], yd[~te])
                p = predict(cfg["family"], m, Xd[te])
                oof[r, te] = p
                accs[r, k] = (p == yd[te]).mean()
                if cfg["family"] == "mlp":
                    qaccs.append((qpredict(m, Xd[te]) == yd[te]).mean())
        full = make(cfg).fit(Xd, yd)
        rm = accs.mean(axis=1)
        fp = footprint(cfg, full, X.shape[1])
        res = {
            "name": cfg["name"], "family": cfg["family"], "params": cfg["params"],
            "cv": {"mean": round(float(rm.mean()), 9), "sd": round(float(rm.std(ddof=1)), 9), "min": round(float(rm.min()), 9),
                   "max": round(float(rm.max()), 9), "repeats_ge_0.90": int((rm >= THRESH).sum()),
                   "repeat_means": [round(float(a), 9) for a in rm], "fold_acc_min": round(float(accs.min()), 9)},
            "footprint": fp,
        }
        res["eligible"] = bool(fp["deployed_bytes"] <= MAX_BYTES and (rm >= THRESH).all())
        if qaccs:
            res["cv_int8_sim_mean"] = round(float(np.mean(qaccs)), 6)
        results.append(res)
        oofs[cfg["name"]] = oof
        models[cfg["name"]] = full
        print(f"{res['name']:12s} mean {res['cv']['mean']:.4f} sd {res['cv']['sd']:.4f} min {res['cv']['min']:.4f} >=0.90 {res['cv']['repeats_ge_0.90']:2d}/10 deployed {fp['deployed_bytes']:6d} B eligible {res['eligible']}", flush=True)
    winner, reps = select(results)
    return {"features": names, "with_band": with_band, "n_features": len(names), "configs": results, "winner": winner,
            "family_representatives": reps, "any_eligible": winner is not None, "oofs": oofs, "models": models, "X": X}


runs = [run(False)]
if not runs[0]["any_eligible"]:
    print("no config eligible with 18 features; rerunning with band energy appended")
    runs.append(run(True))
final = runs[-1]
band_used = final["with_band"]
X_final, names_final = final["X"], final["features"]
res_by = {r["name"]: r for r in final["configs"]}
fallback_pick = final["winner"] is None
win = final["winner"] if not fallback_pick else max(final["configs"], key=lambda r: r["cv"]["mean"])["name"]
reps = final["family_representatives"]
wcfg = cfg_of[win]
wfam = wcfg["family"]
print("winner:", win, "fallback_pick:", fallback_pick)

yd = y[dev_mask]
yho = y[ho_mask]
Xho = X_final[ho_mask]


def evaluate(nm):
    cfg = cfg_of[nm]
    m = final["models"][nm]
    e = {"name": nm, "family": cfg["family"], "cv": res_by[nm]["cv"], "footprint": res_by[nm]["footprint"], "eligible": res_by[nm]["eligible"],
         "oof_pooled_all_repeats": metrics(np.tile(yd, REPEATS), final["oofs"][nm].ravel()),
         "holdout": metrics(yho, predict(cfg["family"], m, Xho))}
    if cfg["family"] == "forest":
        v = rf_votes(m, Xho)
        e["holdout_vote_ties"] = int(((v == v.max(axis=1, keepdims=True)).sum(axis=1) > 1).sum())
        e["holdout_windows"] = int(len(Xho))
    if cfg["family"] == "mlp":
        e["holdout_int8_sim_accuracy"] = round(float((qpredict(m, Xho) == yho).mean()), 6)
    return e


rep_out = {fam: evaluate(nm) for fam, nm in reps.items()}
win_out = rep_out[wfam] if reps[wfam] == win else evaluate(win)
model = make(wcfg).fit(X_final, y)
dep_fp = footprint(wcfg, model, X_final.shape[1])
joblib.dump(model, OUT / "model.joblib")
reloaded = joblib.load(OUT / "model.joblib")
assert np.array_equal(dpredict(wfam, reloaded, X_final), dpredict(wfam, model, X_final))
print("model.joblib reload reproduces full-data model predictions on", len(X_final), "windows")

prev_name = "rf_n20_d4"
prev_out = evaluate(prev_name)
pv = rf_votes(final["models"][prev_name], Xho)
ptie = (pv == pv.max(axis=1, keepdims=True)).sum(axis=1) > 1
pp = predict("forest", final["models"][prev_name], Xho)
prev_out["holdout_tie_true_class_counts"] = {CLASSES[c]: int((yho[ptie] == c).sum()) for c in range(3)}
prev_out["holdout_tie_predicted_class_counts"] = {CLASSES[c]: int((pp[ptie] == c).sum()) for c in range(3)}

elig_names = [r["name"] for r in final["configs"] if r["eligible"]]
assert win in elig_names
all_ids = {c: np.array(sorted(r for r, k in rec_class.items() if k == c)) for c in range(3)}


def fit_acc(nm, te):
    cfg = cfg_of[nm]
    m = make(cfg).fit(X_final[~te], y[~te])
    return float((predict(cfg["family"], m, X_final[te]) == y[te]).mean())


redraw_te = []
for s_ in range(N_REDRAW):
    rr = np.random.default_rng(SEED + 5000 + s_)
    ids = set()
    for c in range(3):
        ids |= {int(i) for i in rr.choice(all_ids[c], 10, replace=False)}
    te = np.isin(rec, list(ids))
    assert len(ids) == 30 and te.sum() == 1200 and all((te & (y == c)).sum() == 400 for c in range(3))
    assert not set(rec[te]) & set(rec[~te])
    redraw_te.append(te)
cvfull_te = []
for r_ in range(N_CV_FULL):
    rr = np.random.default_rng(SEED + 6000 + r_)
    fids = [set() for _ in range(5)]
    for c in range(3):
        perm = rr.permutation(all_ids[c])
        for k in range(5):
            fids[k] |= {int(i) for i in perm[k * 10:(k + 1) * 10]}
    tes = [np.isin(rec, list(f)) for f in fids]
    assert set().union(*fids) == ho_set | dev_set and sum(len(f) for f in fids) == 150
    assert all(t.sum() == 1200 and all((t & (y == c)).sum() == 400 for c in range(3)) for t in tes)
    assert (sum(t.astype(int) for t in tes) == 1).all()
    cvfull_te.append(tes)

raw_redraw, raw_cvfull = {}, {}
for nm in elig_names:
    raw_redraw[nm] = np.array([fit_acc(nm, te) for te in redraw_te])
    raw_cvfull[nm] = np.array([np.mean([fit_acc(nm, te) for te in tes]) for tes in cvfull_te])
    print(f"broad {nm:10s} redraw mean {raw_redraw[nm].mean():.4f} cv-all-data mean {raw_cvfull[nm].mean():.4f}", flush=True)


def bstats(a, w):
    return {"n": int(len(a)), "mean": round(float(a.mean()), 9), "sd": round(float(a.std(ddof=1)), 9), "min": round(float(a.min()), 9),
            "count_ge_0.90": int((a >= THRESH).sum()), "mean_diff_vs_winner": round(float((a - w).mean()), 9),
            "frac_winner_ge": round(float((w >= a).mean()), 6)}


broad = {
    "uses_set_aside_recordings": True, "selection_free": False,
    "note": "uses all 150 recordings including the 30 set-aside holdout recordings; computed after selection; NOT selection-free",
    "redraw": f"{N_REDRAW} holdout re-draws: per seed ({SEED}+5000+i) draw 10 recordings/class from all 50 as test, train on remaining 40/class; RF by hard vote",
    "cv_all_data": f"{N_CV_FULL} repeats (seed {SEED}+6000+i) of grouped stratified 5-fold over all 150 recordings, 10 recordings/class per test fold; repeat value = mean of 5 fold accuracies",
    "configs": {nm: {"redraw": bstats(raw_redraw[nm], raw_redraw[win]), "cv_all_data": bstats(raw_cvfull[nm], raw_cvfull[win])} for nm in elig_names},
}
bw = broad["configs"][win]

w_ho = windows[ho_mask]
rec_ho = rec[ho_mask]
uniq_ho = np.array(sorted(set(rec_ho)))
ho_pos = np.searchsorted(uniq_ho, rec_ho)
Xd_cache = {}
ot_models = []


def add_model(label, nm, with_rms):
    if any(m["name"] == nm and m["with_rms"] == with_rms for m in ot_models):
        return
    cfg = cfg_of[nm]
    if with_rms:
        Xr, _ = compute_feats(windows, with_rms=True, with_band=band_used)
        m = make(cfg).fit(Xr[dev_mask], yd)
    else:
        m = final["models"][nm]
    ot_models.append({"label": label, "name": nm, "family": cfg["family"], "model": m, "with_rms": with_rms,
                      "n_features": X_final.shape[1] + (6 if with_rms else 0)})


add_model(f"{win} (winner)", win, False)
for fam in ["tree", "forest", "mlp"]:
    add_model(f"{reps[fam]} (rep {fam})", reps[fam], False)
add_model("dt_d3", "dt_d3", False)
add_model("dt_d4", "dt_d4", False)
add_model("rf_n20_d4", "rf_n20_d4", False)
add_model("dt_d3 +rms", "dt_d3", True)
add_model("dt_d4 +rms", "dt_d4", True)
add_model("rf_n20_d4 +rms", "rf_n20_d4", True)


def acc_with_offsets(off):
    wp = np.clip(np.rint(w_ho + off[:, :, None]), -32768, 32767)
    feats = {}
    out = {}
    for m in ot_models:
        if m["with_rms"] not in feats:
            feats[m["with_rms"]] = compute_feats(wp, with_rms=m["with_rms"], with_band=band_used)[0]
        out[m["label"]] = float((predict(m["family"], m["model"], feats[m["with_rms"]]) == yho).mean())
    return out


base = acc_with_offsets(np.zeros((len(w_ho), 6)))
draws = []
for d in range(N_OFFSET_DRAWS):
    ro = np.random.default_rng(SEED + 1000 + d)
    off_rec = ro.uniform(-OFFSET_RANGE, OFFSET_RANGE, (len(uniq_ho), 6))
    draws.append(acc_with_offsets(off_rec[ho_pos]))
worst = {}
for sgn in [-OFFSET_RANGE, OFFSET_RANGE]:
    off = np.zeros((len(w_ho), 6))
    off[:, 2] = sgn
    worst[sgn] = acc_with_offsets(off)
offset_rows = []
for m in ot_models:
    lb = m["label"]
    a = np.array([dr[lb] for dr in draws])
    offset_rows.append({
        "label": lb, "config": m["name"], "n_features": m["n_features"], "unperturbed_holdout": round(base[lb], 6),
        "random_mean": round(float(a.mean()), 6), "random_min": round(float(a.min()), 6),
        "drop_mean": round(base[lb] - float(a.mean()), 6), "drop_max": round(base[lb] - float(a.min()), 6),
        "az_minus_1000": round(worst[-OFFSET_RANGE][lb], 6), "az_plus_1000": round(worst[OFFSET_RANGE][lb], 6),
        "az_worst_drop": round(base[lb] - min(worst[-OFFSET_RANGE][lb], worst[OFFSET_RANGE][lb]), 6),
    })
print("\noffset test (holdout, unperturbed / random mean / random min / az-1000 / az+1000)")
for o in offset_rows:
    print(f"{o['label']:24s} {o['n_features']:3d}f {o['unperturbed_holdout']:.4f} {o['random_mean']:.4f} {o['random_min']:.4f} {o['az_minus_1000']:.4f} {o['az_plus_1000']:.4f}")

export = {
    "model_type": {"tree": "decision_tree", "forest": "random_forest", "mlp": "mlp_int8"}[wfam],
    "config": win, "params": wcfg["params"],
    "class_order": CLASSES, "input_features": names_final, "n_input_features": len(names_final),
    "comparison_rule": "go left if x[feature] <= threshold else right; thresholds are float32 (rounded down so float32 x <= threshold matches sklearn exactly); inputs cast to float32",
}
used_idx = []
if wfam in ("tree", "forest"):
    ests = [model] if wfam == "tree" else list(model.estimators_)
    trees = []
    for e in ests:
        t = e.tree_
        thr = t.threshold.astype(np.float32)
        fix = thr.astype(np.float64) > t.threshold
        thr[fix] = np.nextafter(thr[fix], np.float32(-np.inf))
        leaf = t.children_left == -1
        lc = leaf_classes(t, e)
        feat = np.where(leaf, -1, t.feature)
        trees.append({
            "n_nodes": int(t.node_count),
            "feature_index": feat.tolist(),
            "feature_name": [None if f < 0 else names_final[f] for f in feat],
            "threshold": [None if lf else float(v) for lf, v in zip(leaf, thr)],
            "left": np.where(leaf, -1, t.children_left).tolist(),
            "right": np.where(leaf, -1, t.children_right).tolist(),
            "leaf_class": [int(c) if lf else None for lf, c in zip(leaf, lc)],
        })
    if wfam == "forest":
        export["vote_rule"] = "each tree outputs its leaf class; majority vote over trees; ties -> lowest class index"
    else:
        export["vote_rule"] = "n/a (single tree: output the leaf class)"
    export["trees"] = trees
    used_idx = sorted({f for tr in trees for f in tr["feature_index"] if f >= 0})

    def np_eval(X):
        X32 = X.astype(np.float32)
        votes = np.zeros((len(X), 3), dtype=int)
        for tr in trees:
            feat = np.array(tr["feature_index"])
            thr = np.array([0.0 if v is None else v for v in tr["threshold"]], dtype=np.float32)
            left, right = np.array(tr["left"]), np.array(tr["right"])
            lcl = np.array([-1 if v is None else v for v in tr["leaf_class"]])
            node = np.zeros(len(X), dtype=int)
            while True:
                leaf = left[node] == -1
                if leaf.all():
                    break
                xv = X32[np.arange(len(X)), np.where(leaf, 0, feat[node])]
                node = np.where(leaf, node, np.where(xv <= thr[node], left[node], right[node]))
            votes[np.arange(len(X)), lcl[node]] += 1
        return np.argmax(votes, axis=1)
else:
    mlp = model[1]
    layers = []
    for w, b in zip(mlp.coefs_, mlp.intercepts_):
        s = np.abs(w).max() / 127
        layers.append({"weights_int8": np.round(w / s).astype(int).tolist(), "scale": float(s), "bias": b.tolist(), "shape": list(w.shape)})
    export.update({"scaler_mean": model[0].mean_.tolist(), "scaler_scale": model[0].scale_.tolist(), "layers": layers,
                   "activation": "relu hidden layers, argmax output", "dequantization": "W = weights_int8 * scale"})
    used_idx = list(range(len(names_final)))

    def np_eval(X):
        a = (X - np.array(export["scaler_mean"])) / np.array(export["scaler_scale"])
        for i, ly in enumerate(layers):
            a = a @ (np.array(ly["weights_int8"], dtype=np.float64) * ly["scale"]) + np.array(ly["bias"])
            if i < len(layers) - 1:
                a = np.maximum(a, 0)
        return np.argmax(a, axis=1)


used_names = [names_final[i] for i in used_idx]
axes_needed = [a for a in AXES if any(n.startswith(a + "_") for n in used_names)]
needed = {a: [n[len(a) + 1:] for n in used_names if n.startswith(a + "_")] for a in axes_needed}
export["used_feature_indices"] = used_idx
export["used_feature_names"] = used_names
export["firmware_must_compute"] = needed
export["firmware_axes_needed"] = axes_needed
export["firmware_needs_fft_axes"] = [a for a in axes_needed if "domfreq" in needed[a] or any(k.startswith("band") for k in needed[a])]
assert np.array_equal(np_eval(X_final), dpredict(wfam, model, X_final))
print("export numpy evaluation reproduces full-data model predictions on", len(X_final), "windows")
(OUT / "model_export.json").write_text(json.dumps(export, indent=1))

formulas = {
    "tree": "bytes = 10 * node_count (feature idx u8 + threshold f32 + left int16 + right int16 + class/leaf u8); int16-threshold variant = 8 * node_count; forest = sum over trees; ops = sum of max tree depths",
    "mlp": "int8_bytes = weights*1 + biases*4 + layers*4 (one f32 scale per layer) + n_features*8 (scaler mean+scale f32); ops = MAC count = weights",
    "forest_inference": "one class per leaf, hard majority vote, ties -> lowest class index (all forest accuracies use this); single tree: leaf class",
}
ranked = sorted([r for r in final["configs"] if r["eligible"]], key=lambda r: (r["footprint"]["deployed_bytes"], -r["cv"]["mean"]))
cv_w = res_by[win]["cv"]
fp_w = res_by[win]["footprint"]["deployed_bytes"]
ho_w = win_out["holdout"]["accuracy"]
ho_p = prev_out["holdout"]["accuracy"]
bp = broad["configs"][prev_name]
ratio = round(res_by[prev_name]["footprint"]["deployed_bytes"] / fp_w)
just = [f"Winner {win}: smallest deployed footprint ({fp_w} B, limit {MAX_BYTES} B) among {len(ranked)} eligible configs (eligible = footprint <= {MAX_BYTES} B and all {REPEATS} dev repeat means >= {THRESH}); dev repeated-CV mean {cv_w['mean']:.4f}, sd {cv_w['sd']:.4f}, min repeat {cv_w['min']:.4f}, {cv_w['repeats_ge_0.90']}/{REPEATS} repeats >= {THRESH}."]
for ru in ranked[1:]:
    just.append(f"Eligible {ru['name']}: {ru['footprint']['deployed_bytes']} B ({ru['footprint']['deployed_bytes'] - fp_w:+d} B vs winner), dev CV mean {ru['cv']['mean']:.4f} ({ru['cv']['mean'] - cv_w['mean']:+.4f}).")
for fam, nm in reps.items():
    if nm == win:
        continue
    o = res_by[nm]
    just.append(f"vs {fam} representative {nm}: {o['footprint']['deployed_bytes'] - fp_w:+d} B, {o['cv']['mean'] - cv_w['mean']:+.4f} dev CV mean, min repeat {o['cv']['min']:.4f}, {o['cv']['repeats_ge_0.90']}/{REPEATS} repeats >= {THRESH}, eligible {o['eligible']}.")
just.append(f"Broad resampling (uses set-aside recordings, NOT selection-free): {win} {bw['redraw']['mean']:.4f} over {N_REDRAW} holdout re-draws, {bw['cv_all_data']['mean']:.4f} over {N_CV_FULL} all-data CV repeats.")
just.append(f"{prev_name} vs {win}: re-draws {bp['redraw']['mean']:.4f} vs {bw['redraw']['mean']:.4f}; all-data CV {bp['cv_all_data']['mean']:.4f} vs {bw['cv_all_data']['mean']:.4f}; {win} >= {prev_name} in {bp['cv_all_data']['frac_winner_ge']:.0%} of CV repeats.")
just.append(f"Fixed set-aside holdout for {win}: {ho_w:.4f} (supporting evidence only; these recordings helped drive the switch from {prev_name} ({ho_p:.4f}), so it is not an untouched holdout estimate).")
if ho_w < THRESH:
    just.append("FINDING: winner holdout accuracy is below 0.90; selection was not changed.")
wo = next(o for o in offset_rows if o["label"] == f"{win} (winner)")
just.append(f"Offset test (holdout, +/-{OFFSET_RANGE} count per-axis offsets): winner mean drop {wo['drop_mean']:.4f}, max drop {wo['drop_max']:.4f}, worst az +/-1000 drop {wo['az_worst_drop']:.4f}. Scope: see offset_scope.")

offset_scope = ("The offset test shows the 18 mean-removed features are invariant to constant per-axis offsets BY CONSTRUCTION. It does NOT test axis rotation / mounting orientation changes, sensitivity/gain (scale) errors, or temperature drift; these are open questions for Phase 6 hardware validation.")
tie_note = f"{prev_name} hard-vote ties on the fixed holdout: {prev_out['holdout_vote_ties']} of {prev_out['holdout_windows']} windows; true classes of tied windows {prev_out['holdout_tie_true_class_counts']}, predicted {prev_out['holdout_tie_predicted_class_counts']} (lowest-index tie rule breaks toward normal)."
v2_to_v3 = (f"the RF dev-CV lead did not survive broad resampling ({prev_name} {bp['redraw']['mean']:.4f} vs {win} {bw['redraw']['mean']:.4f} over {N_REDRAW} re-draws; {bp['cv_all_data']['mean']:.4f} vs {bw['cv_all_data']['mean']:.4f} over {N_CV_FULL} all-data CV repeats; {win} >= {prev_name} in {bp['cv_all_data']['frac_winner_ge']:.0%} of CV repeats; fixed holdout {ho_p:.4f} vs {ho_w:.4f}); "
            f"RF hard-vote ties break toward normal by the lowest-index rule; {prev_name} is {ratio}x the size")
history = [
    {"version": "v1", "config": "dt_d4", "features": 24, "cv": "single 5-fold fold assignment", "rule": "smallest footprint with CV mean >= 0.90",
     "reason_for_change": "single-assignment luck (dt_d3 missed 0.90 at 0.8975 only on that one fold assignment) and az_rms offset fragility (24-feature trees split on DC-dominated az_rms; rms dropped)"},
    {"version": "v2", "config": "rf_n20_d4", "features": 18, "cv": f"{REPEATS}x repeated grouped 5-fold on dev", "rule": f"highest mean among configs <= {MAX_BYTES} B with all repeat means >= {THRESH}",
     "reason_for_change": v2_to_v3},
    {"version": "v3", "config": win, "features": 18, "cv": f"{REPEATS}x repeated grouped 5-fold on dev", "rule": f"smallest deployed footprint among configs <= {MAX_BYTES} B with all repeat means >= {THRESH}; ties -> higher mean",
     "reason_for_change": None},
    {"version": "v4", "config": win, "features": 18, "cv": "none (no selection change)", "rule": "deployment: same config retrained on all 150 recordings",
     "reason_for_change": None},
]

deployed = {
    "trained_on": "all 150 recordings (6000 windows), 18 features, same config and random_state as the selected config",
    "config": win, "params": wcfg["params"], "footprint": dep_fp, "deployed_bytes": dep_fp["deployed_bytes"],
    "used_features": used_names, "firmware_axes_needed": axes_needed, "firmware_needs_fft_axes": export["firmware_needs_fft_axes"],
    "dev_trained_footprint": res_by[win]["footprint"],
    "untouched_evaluation_data": False,
    "accuracy_estimate": f"no untouched evaluation data exists for this model; its accuracy estimate is the broad-resampling estimate of the training procedure (re-draws {bw['redraw']['mean']:.4f}, all-data CV {bw['cv_all_data']['mean']:.4f}); Phase 6 hardware data is its real test",
    "scope_note": "the fixed-holdout metrics, family-representative metrics and the offset test describe the dev-trained model (120 recordings), not this model",
    "alternatives": "see models/selected/comparison.md for the 24-config comparison and the documented fallback rf_n50_d4",
}


def strip(r):
    return {k: v for k, v in r.items() if k not in ("models", "oofs", "X")}


report = {
    "seed": SEED, "fs": FS, "n": N, "classes": CLASSES, "cv_threshold": THRESH, "max_deployed_bytes": MAX_BYTES, "repeats": REPEATS,
    "selection_rule": f"eligible = deployed bytes <= {MAX_BYTES} and every one of {REPEATS} repeat means (each = mean of 5 fold accuracies) >= {THRESH}; winner = smallest deployed footprint among eligible, ties -> higher overall mean; family representative by same rule within family, else highest overall mean; sd is ddof=1 over repeat means",
    "headline": {"winner": win, "redraw_mean": bw["redraw"]["mean"], "cv_all_data_mean": bw["cv_all_data"]["mean"],
                 "note": "broad-resampling estimates use the set-aside recordings and are NOT selection-free", "fixed_set_aside_holdout": ho_w,
                 "holdout_caveat": f"the set-aside recordings were part of the evidence that drove the switch from {prev_name} to {win}; the fixed holdout is supporting evidence, NOT an untouched holdout estimate"},
    "deployed_model": deployed,
    "broad_resampling": broad,
    "selection_history": history,
    "previous_winner_rf_n20_d4": prev_out,
    "previous_winner_tie_note": tie_note,
    "offset_scope": offset_scope,
    "split": split, "holdout_assignment": {CLASSES[c]: holdout_ids[c] for c in range(3)},
    "fold_assignment_by_repeat": [{"repeat": r + 1, "seed": SEED + 1 + r, "folds": fold_assign[r]} for r in range(REPEATS)],
    "oof_note": "family representative OOF metrics pool out-of-fold predictions from all repeats",
    "formulas": formulas, "band_energy_used": band_used,
    "band_energy_note": "no config eligible with 18 features; second run appended 18 band-energy features" if band_used else "not needed: at least one config eligible with 18 features",
    "runs": [strip(r) for r in runs],
    "winner": {**win_out, "fallback_pick": fallback_pick, "n_features": final["n_features"]},
    "family_representatives": rep_out,
    "offset_test": {"description": f"per-recording constant offset U(-{OFFSET_RANGE},{OFFSET_RANGE}) counts on all 6 axes added to holdout raw windows, clipped to int16, features recomputed; {N_OFFSET_DRAWS} seeded draws (seed {SEED}+1000+d); deterministic worst case az -1000 / +1000",
                    "rows": offset_rows},
    "used_features": used_names, "firmware_must_compute": needed,
    "justification": just,
}
(OUT / "report.json").write_text(json.dumps(report, indent=2))

md = ["# Resonance Phase 3 report", "", f"## Headline: {win} accuracy (broad resampling)", "",
      f"- {N_REDRAW} random holdout re-draws (10 rec/class test, 40 rec/class train, all 150 recordings): mean {bw['redraw']['mean']:.4f}, sd {bw['redraw']['sd']:.4f}, min {bw['redraw']['min']:.4f}, {bw['redraw']['count_ge_0.90']}/{N_REDRAW} >= 0.90",
      f"- {N_CV_FULL} repeats of grouped stratified 5-fold CV over all 150 recordings: mean {bw['cv_all_data']['mean']:.4f}, sd {bw['cv_all_data']['sd']:.4f}, min {bw['cv_all_data']['min']:.4f}",
      "- Both estimates use the 30 set-aside recordings and are therefore NOT selection-free.",
      f"- Supporting evidence: fixed set-aside holdout {ho_w:.4f} (cm {win_out['holdout']['confusion_matrix']}). The set-aside recordings were part of the evidence that drove the switch from {prev_name} ({ho_p:.4f}) to {win}, so this is NOT an untouched holdout estimate.",
      f"- Dev-only {REPEATS}x repeated CV (the data used for selection): mean {cv_w['mean']:.4f}, sd {cv_w['sd']:.4f}, min {cv_w['min']:.4f}.", "",
      "## Deployed model", "",
      f"- {win} retrained on ALL 150 recordings (6000 windows), 18 input features; this is the model in model.joblib / model_export.json.",
      f"- structure: {dep_fp.get('nodes', 'n/a')} nodes, {dep_fp['deployed_bytes']} B deployed (dev-trained {win} was {res_by[win]['footprint'].get('nodes', 'n/a')} nodes, {fp_w} B).",
      f"- used features ({len(used_names)}): {', '.join(used_names)}", f"- axes needed: {', '.join(axes_needed)}; FFT axes: {', '.join(export['firmware_needs_fft_axes'])}",
      "- It has no untouched evaluation data. Its accuracy estimate is the broad-resampling estimate of the training procedure above (about " + f"{bw['cv_all_data']['mean']:.2f}" + "); Phase 6 hardware data is its real test.",
      "- The fixed-holdout metrics, family-representative metrics and offset test in this report describe the dev-trained model (120 recordings), not the deployed model.",
      "- Alternatives and the documented fallback rf_n50_d4: see comparison.md.", "",
      "## Selection history", ""]
for h in history:
    md.append(f"- {h['version']}: {h['config']}, {h['features']} features, {h['cv']}, rule: {h['rule']}." + (f" Changed because: {h['reason_for_change']}." if h["reason_for_change"] else (" (current selection)" if h["version"] == "v3" else " (current deployed model; not a selection change)")))
md += ["", "## Scope of the offset test", "", offset_scope, "",
      "## Broad resampling (all eligible configs; uses set-aside recordings, NOT selection-free)", "",
      f"Re-draws: {N_REDRAW} seeds ({SEED}+5000+i); per seed 10 recordings/class test from all 50, train on remaining 40/class; RF by hard vote. All-data CV: {N_CV_FULL} repeats (seed {SEED}+6000+i) of grouped stratified 5-fold over all 150 recordings. Diff = config minus winner (paired); winner>= = fraction of re-draws/repeats where the winner is at least as accurate.", "",
      "| config | deployed B | redraw mean | sd | min | >=0.90 | diff vs winner | winner>= | allCV mean | sd | min | diff vs winner | winner>= |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
for nm in elig_names:
    rd, cv = broad["configs"][nm]["redraw"], broad["configs"][nm]["cv_all_data"]
    md.append(f"| {nm}{' (winner)' if nm == win else ''} | {res_by[nm]['footprint']['deployed_bytes']} | {rd['mean']:.4f} | {rd['sd']:.4f} | {rd['min']:.4f} | {rd['count_ge_0.90']}/{N_REDRAW} | {rd['mean_diff_vs_winner']:+.4f} | {rd['frac_winner_ge']:.2f} | {cv['mean']:.4f} | {cv['sd']:.4f} | {cv['min']:.4f} | {cv['mean_diff_vs_winner']:+.4f} | {cv['frac_winner_ge']:.2f} |")
md += ["", f"## Previous winner {prev_name} (fixed holdout)", "", f"- holdout acc {ho_p:.4f}, cm {prev_out['holdout']['confusion_matrix']}", f"- {tie_note}", "",
       f"Band energy used: {band_used}", f"Input features: {final['n_features']} ({', '.join(names_final)})", "", "## Split", ""]
for nm in ["dev", "holdout"]:
    md.append(f"- {nm}: " + ", ".join(f"{c} {v['recordings']} rec / {v['windows']} win" for c, v in split[nm].items()))
md += [f"- {REPEATS} repeats of grouped stratified 5-fold on dev (fold seeds {SEED + 1}..{SEED + REPEATS}); every fold: train 32 rec/class (1280 win/class), test 8 rec/class (320 win/class); fold ids in report.json", "",
       f"## Configs (repeated CV on dev; eligible = footprint <= {MAX_BYTES} B and all {REPEATS} repeat means >= {THRESH})", "",
       "| config | deployed B | mean | sd | min | repeats >= 0.90 | eligible |", "|---|---|---|---|---|---|---|"]
for r in final["configs"]:
    md.append(f"| {r['name']} | {r['footprint']['deployed_bytes']} | {r['cv']['mean']:.4f} | {r['cv']['sd']:.4f} | {r['cv']['min']:.4f} | {r['cv']['repeats_ge_0.90']}/{REPEATS} | {r['eligible']} |")
md += ["", "## Family representatives (OOF pooled over all repeats; holdout afterwards)", ""]
for fam in ["tree", "forest", "mlp"]:
    r = rep_out[fam]
    md += [f"### {fam}: {r['name']}", f"- CV mean {r['cv']['mean']:.4f}, sd {r['cv']['sd']:.4f}, min {r['cv']['min']:.4f}, {r['cv']['repeats_ge_0.90']}/{REPEATS} repeats >= 0.90, deployed {r['footprint']['deployed_bytes']} B, eligible {r['eligible']}",
           f"- OOF acc {r['oof_pooled_all_repeats']['accuracy']:.4f}, precision {r['oof_pooled_all_repeats']['precision']}, recall {r['oof_pooled_all_repeats']['recall']}, cm {r['oof_pooled_all_repeats']['confusion_matrix']}",
           f"- holdout acc {r['holdout']['accuracy']:.4f}, precision {r['holdout']['precision']}, recall {r['holdout']['recall']}, cm {r['holdout']['confusion_matrix']}"]
    if "holdout_vote_ties" in r:
        md.append(f"- holdout vote ties: {r['holdout_vote_ties']} of {r['holdout_windows']} windows")
    if fam == "mlp":
        md.append(f"- holdout int8-simulated accuracy {r['holdout_int8_sim_accuracy']:.4f}")
    md.append("")
md += [f"## Winner: {win} (selection; dev-trained numbers)", f"- dev CV mean {cv_w['mean']:.4f}, sd {cv_w['sd']:.4f}, min {cv_w['min']:.4f}, fixed set-aside holdout {win_out['holdout']['accuracy']:.4f} (not untouched), deployed {res_by[win]['footprint']['deployed_bytes']} B",
       f"- deployed (all-data) model used features ({len(used_names)}): {', '.join(used_names)}", f"- firmware must compute: {needed}", "", "## Justification", ""] + [f"- {j}" for j in just]
md += ["", "## Offset robustness (holdout)", "", f"Per-recording offset U(-{OFFSET_RANGE},{OFFSET_RANGE}) counts on every axis, {N_OFFSET_DRAWS} draws; az worst case = constant -1000 / +1000 on az. {offset_scope}", "",
       "| model | features | unperturbed | random mean | random min | drop mean | drop max | az -1000 | az +1000 |", "|---|---|---|---|---|---|---|---|---|"]
for o in offset_rows:
    md.append(f"| {o['label']} | {o['n_features']} | {o['unperturbed_holdout']:.4f} | {o['random_mean']:.4f} | {o['random_min']:.4f} | {o['drop_mean']:.4f} | {o['drop_max']:.4f} | {o['az_minus_1000']:.4f} | {o['az_plus_1000']:.4f} |")
md += ["", "## Footprint formulas", ""] + [f"- {k}: {v}" for k, v in formulas.items()]
(OUT / "report.md").write_text("\n".join(md) + "\n")

spec = {
    "fs_hz": FS, "window_n": N, "axis_order": AXES, "classes": CLASSES,
    "input_units": "raw int16 counts as read from MPU6050 (no scaling to g or deg/s)",
    "window_layout": "6 axes, 128 consecutive samples each, non-overlapping windows",
    "input_columns": names_final, "n_input_columns": len(names_final),
    "used_feature_columns": used_names, "firmware_must_compute": needed,
    "formulas": {
        "std": "sqrt(mean((x-mean(x))^2)), population (ddof=0), mean removed",
        "kurt": "excess kurtosis: mean((x-mu)^4)/var^2 - 3 with population moments; 0 if var==0",
        "domfreq": "(argmax over k=1..64 of |rfft(x-mean(x))[k]|) * FS / N (bin 0 excluded; first max wins ties)",
    },
    "sensor_configuration": {
        "accel_full_scale": "+/-2 g (AFS_SEL=0, 16384 LSB/g)", "gyro_full_scale": "+/-250 deg/s (FS_SEL=0, 131 LSB/(deg/s))",
        "note": "defaults set by electroniccats MPU6050 initialize(); all thresholds are in raw counts under these settings and are invalid under other ranges"},
    "sampling_requirement": {
        "fs_hz": FS, "uniform": True, "timer_driven": True, "domfreq_bin_spacing_hz": FS / N,
        "note": "exactly 100 Hz, uniformly spaced (timer-driven); domfreq thresholds assume bin spacing FS/N = 0.78125 Hz. The current Phase 1 firmware uses delay(10) plus read/print time, so its actual rate is below 100 Hz and must be changed in Phase 4"},
    "offset_test_scope": offset_scope,
    "used_features": used_names,
    "firmware_axes_needed": export["firmware_axes_needed"],
    "firmware_needs_fft_axes": export["firmware_needs_fft_axes"],
    "rms_excluded": "raw rms includes the DC term (gravity, mounting orientation, sensor offset/calibration), which differs on real hardware; the 24-feature model's top split was on az_rms (DC dominated). rms is intentionally not an input.",
    "band_energy": None,
    "comparison_rule": export["comparison_rule"],
    "vote_rule": export.get("vote_rule"),
    "model": {"name": win, "family": wfam, "file": "model.joblib", "export_file": "model_export.json", "trained_on": "all 150 recordings"},
    "scaler": None,
}
if band_used:
    spec["band_energy"] = {
        "formula": "X = rfft(x-mean(x)); energy = sum(|X_k|^2)/N over bins; bin freq = k*FS/N",
        "low": "1 <= k, f < 4 Hz", "mid": "4 <= f < 14 Hz", "high": "f >= 14 Hz through Nyquist (k=64)",
    }
if wfam == "mlp":
    spec["scaler"] = {"mean": model[0].mean_.tolist(), "scale": model[0].scale_.tolist()}
(OUT / "feature_spec.json").write_text(json.dumps(spec, indent=2))

print()
for fam in ["tree", "forest", "mlp"]:
    r = rep_out[fam]
    print(f"{fam}: {r['name']} cv {r['cv']['mean']:.4f} sd {r['cv']['sd']:.4f} min {r['cv']['min']:.4f} deployed {r['footprint']['deployed_bytes']} B holdout {r['holdout']['accuracy']:.4f}" + (f" ties {r['holdout_vote_ties']}" if "holdout_vote_ties" in r else ""))
print("band energy used:", band_used)
print("winner:", win, "cv", cv_w["mean"], "sd", cv_w["sd"], "min", cv_w["min"], "repeats>=0.90", cv_w["repeats_ge_0.90"], "holdout", win_out["holdout"]["accuracy"], "deployed B", res_by[win]["footprint"]["deployed_bytes"])
print("winner holdout cm:", win_out["holdout"]["confusion_matrix"])
print("used features:", used_names)
print("\n".join(just))
