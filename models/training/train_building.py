"""T1b: retrain the window classifier on simulated-building data and decide, with a fixed rule, whether it replaces the
deployed tree (models/selected). Output: models/selected_building/ (model.joblib, model_export.json, feature_spec.json,
report.json, report.md). Nothing under models/selected/ or firmware/ is touched.

Design fixed in this file before any decision-set result was looked at (this is NOT described as pre-registered):
  data      simulated buildings (building_sim), seeds > 4,000,000, disjoint from every earlier gate block; all 10 floors.
  labels    normal: healthy ambient (level 0.5x-1.5x of commissioning), noise-only windows, and every window of an event
            scenario that is not an event on the event floor; abnormal: building-wide 3x / 5x excitation, all floors;
            faulty: windows of the single event floor that contain an impact (incl. decay down to an envelope of
            IMPACT_ENVELOPE_CUTOFF counts) or the frequency-shift signature. Stiffness loss and 2x are never trained on.
  features  Set A (absolute): az_std az_kurt az_domfreq gx_std gx_kurt gx_domfreq; Set B: A + az_std_rel gx_std_rel
            (value / the floor's commissioning median from a separate healthy recording).
  selection development buildings only; 10x repeated grouped (by building) 5-fold; train.py's DT and RF grid, footprint
            function and rule (smallest footprint <= 5120 B with all repeat means >= 0.90); per feature set, then the smaller
            footprint across sets (tie: Set A).
  decision  fresh held-out buildings: false alarm rate (healthy + noise-only windows, mean over floors, after 2-of-3 smoothing,
            lower is better) and local-event detection (event-floor event windows reported faulty after smoothing), against the
            old tree with the az_std noise gate. REPLACE only if FA is lower AND detection is >= the old tree's.
Re-runnable and resumable: simulation chunks and CV results are cached under CACHE (keys include scenario, damage, window
counts, seeds and the simulator/feature source hashes).
"""
import argparse
import hashlib
import inspect
import json
import sys
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.tree import DecisionTreeClassifier

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "data" / "generator"))
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import building_sim as bs  # noqa: E402
import localisation_gate as lg  # noqa: E402  (old tree, smoothing, window feature code)
import localisation_gate_v2 as lg2  # noqa: E402  (seed blocks only)

SCRATCH = Path(r"C:\Users\raksh\AppData\Local\Temp\claude\121eb6b4f822bd15\8c5ad4ff-5fff-4065-86ee-8a50f4233411\scratchpad\t1b")
OUT_DIR = ROOT / "models" / "selected_building"
SEED = 42
FS, N, NF = 100, 128, 10
CLASSES = ["normal", "abnormal", "faulty"]
THRESH, MAX_BYTES = 0.90, 5120
BASE_FEATS = ["az_std", "az_kurt", "az_domfreq", "gx_std", "gx_kurt", "gx_domfreq"]
SETS = {"A": BASE_FEATS, "B": BASE_FEATS + ["az_std_rel", "gx_std_rel"]}
CURRENT_FIVE = ["az_std", "az_kurt", "az_domfreq", "gx_std", "gx_domfreq"]
CHUNK = 20
N_BOOT = 2000

# seed blocks (> 4,000,000; everything earlier ends at 3,200,200)
BLOCKS = {"dev": (4_100_000, 240), "held": (4_200_000, 150), "small_dev": (4_900_000, 16), "small_held": (4_950_000, 12)}
DEV_SPEC = {"nw": 12, "comm_nw": 20, "n_imp": 3, "n_shift": 3, "extra": False}
HELD_SPEC = {"nw": 30, "comm_nw": 20, "n_imp": 1, "n_shift": 1, "extra": True}
SMALL_HELD_SPEC = dict(HELD_SPEC, nw=12)
EXC = {"comm": 0, "healthy": 1, "noise": 2, "s3": 3, "s5": 4, "s2": 5, "dmg": 6, "imp": 7, "shift": 10}
STRONG = {"s3": 3.0, "s5": 5.0, "s2": 2.0}
DAMAGE_LOSS = 0.30
LOG_PATH = SCRATCH / "train_building.log"
_LOG_ON = True


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    if _LOG_ON:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + s + "\n")
        except OSError:
            pass


def jdump(obj, path):
    path.write_text(json.dumps(obj, indent=1, default=lambda o: o.tolist() if isinstance(o, np.ndarray) else o.item() if isinstance(o, np.generic) else str(o)))


# ------------------------------------------------------------------ seeds
def prior_max_seed():
    first = max(lg.BASE.values()) + 1300
    v2 = max(a + n for a, n in lg2.SEEDS.values())
    return max(first, lg2.FIRST_GATE_MAX, v2)


def assert_seed_blocks_disjoint():
    prior = prior_max_seed()
    rng = sorted((a, a + n, k) for k, (a, n) in BLOCKS.items())
    assert rng[0][0] > 4_000_000 and rng[0][0] >= prior, (rng[0], prior)
    assert all(rng[i][1] <= rng[i + 1][0] for i in range(len(rng) - 1)), "train_building seed blocks overlap"
    return prior


def seeds_of(name):
    a, n = BLOCKS[name]
    return list(range(a, a + n))


# ------------------------------------------------------------------ features
def feature6(az_w, gx_w):
    """(..., 128) int windows -> (..., 6) float64: az_std az_kurt az_domfreq gx_std gx_kurt gx_domfreq (feature_spec.json formulas)."""
    a_std, a_kurt, a_dom = lg.window_features(az_w)
    g_std, g_kurt, g_dom = lg.window_features(gx_w)
    return np.stack([a_std, a_kurt, a_dom, g_std, g_kurt, g_dom], axis=-1)


def make_X(F, comm, names):
    """F (..., 6), comm broadcastable to (..., 2) [commissioning az_std, gx_std] -> (n, len(names))."""
    cols = []
    for nm in names:
        if nm in BASE_FEATS:
            cols.append(F[..., BASE_FEATS.index(nm)])
        elif nm == "az_std_rel":
            cols.append(F[..., 0] / comm[..., 0])
        elif nm == "gx_std_rel":
            cols.append(F[..., 3] / comm[..., 1])
        else:
            raise KeyError(nm)
    return np.stack(cols, axis=-1).reshape(-1, len(names))


# ------------------------------------------------------------------ simulation of one chunk of buildings
def floor_for(seed, kind, k):
    """Event floor (1-based), balanced over buildings, from the seed alone."""
    return (int(seed) + 3 * k + (5 if kind == "shift" else 0)) % NF + 1


def level_mult(seed, exc):
    return float(np.random.default_rng([0xA, int(seed), int(exc)]).uniform(0.5, 1.5))


def damage_storey(seed):
    return int(np.random.default_rng([0xD, int(seed)]).integers(1, NF + 1))


def chunk_key(seeds, spec):
    src = hashlib.sha256()
    src.update(Path(bs.__file__).read_bytes())
    src.update(inspect.getsource(lg.window_features).encode())
    src.update(inspect.getsource(feature6).encode())
    src.update(inspect.getsource(simulate_chunk).encode())
    src.update(json.dumps({"seeds": [int(s) for s in seeds], "spec": spec, "exc": EXC, "strong": STRONG, "damage_loss": DAMAGE_LOSS,
                           "scenario": "default excitation, level 0.5-1.5 per recording (healthy/impact/shift), 3x/5x/2x exact, damage on one storey",
                           "version": 1}, sort_keys=True).encode())
    return src.hexdigest()


def simulate_chunk(seeds, spec):
    ps = [bs.building_params(s) for s in seeds]
    nb, nw = len(ps), spec["nw"]
    out = {"seeds": np.array(seeds)}

    def sim(exc, nwin, amp=1.0, damages=None, mult=None):
        sig = bs.simulate_batch(ps, exc, nwin, damages, "default", amp)
        return sig if mult is None else sig * mult[:, None, None]

    def feats(sig, exc, nwin, kind=None, k=0):
        F = np.empty((nb, NF, nwin, 6))
        floors = np.zeros(nb, dtype=int)
        masks = np.zeros((nb, nwin), dtype=bool)
        for i, p in enumerate(ps):
            kw = {}
            if kind == "imp":
                floors[i] = floor_for(p["seed"], "imp", k)
                kw["impact_floor"] = int(floors[i])
            elif kind == "shift":
                floors[i] = floor_for(p["seed"], "shift", k)
                kw["shift_floor"] = int(floors[i])
            az, gx, tr = bs.sensor_output(p, sig[i], exc, return_truth=True, **kw)
            F[i] = feature6(bs.to_windows(az), bs.to_windows(gx))
            m = tr["impact_mask"] if kind == "imp" else tr["shift_mask"] if kind == "shift" else None
            if m is not None:
                masks[i] = m
        return F, floors, masks

    Fc, _, _ = feats(sim(EXC["comm"], spec["comm_nw"]), EXC["comm"], spec["comm_nw"])
    out["comm"] = np.stack([np.median(Fc[..., 0], axis=2), np.median(Fc[..., 3], axis=2)], axis=-1)
    mh = np.array([level_mult(p["seed"], EXC["healthy"]) for p in ps])
    out["F_healthy"], _, _ = feats(sim(EXC["healthy"], nw, mult=mh), EXC["healthy"], nw)
    out["m_healthy"] = mh
    out["F_noise"], _, _ = feats(np.zeros((nb, NF, nw * N)), EXC["noise"], nw)
    for nm in ("s3", "s5") + (("s2",) if spec["extra"] else ()):
        out["F_" + nm], _, _ = feats(sim(EXC[nm], nw, amp=STRONG[nm]), EXC[nm], nw)
    if spec["extra"]:
        st = np.array([damage_storey(p["seed"]) for p in ps])
        out["F_dmg"], _, _ = feats(sim(EXC["dmg"], nw, damages=[(int(s), DAMAGE_LOSS) for s in st]), EXC["dmg"], nw)
        out["storey_dmg"] = st
    for kind in ("imp", "shift"):
        for k in range(spec["n_" + kind]):
            exc = EXC[kind] + k
            m = np.array([level_mult(p["seed"], exc) for p in ps])
            F, fl, mk = feats(sim(exc, nw, mult=m), exc, nw, kind, k)
            out[f"F_{kind}{k}"], out[f"floor_{kind}{k}"], out[f"mask_{kind}{k}"], out[f"m_{kind}{k}"] = F, fl, mk, m
    return out


def load_block(seeds, spec, cache_dir, tag):
    """Simulate (or load from cache) in chunks of CHUNK buildings and concatenate."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for i in range(0, len(seeds), CHUNK):
        sd = seeds[i:i + CHUNK]
        key = chunk_key(sd, spec)
        f = cache_dir / f"{tag}_{sd[0]}_{len(sd)}_{key[:16]}.npz"
        z = None
        if f.exists():
            try:
                z = dict(np.load(f))
                assert str(z["key"]) == key and np.array_equal(z["seeds"], sd)
            except Exception:  # noqa: BLE001 - corrupt or stale cache file: recompute
                z = None
        if z is None:
            t = time.time()
            z = simulate_chunk(sd, spec)
            np.savez(f, key=np.array(key), **z)
            log(f"  [{tag}] simulated chunk {i // CHUNK + 1}/{-(-len(seeds) // CHUNK)} ({len(sd)} buildings) in {time.time() - t:.0f}s")
        else:
            log(f"  [{tag}] chunk {i // CHUNK + 1}/{-(-len(seeds) // CHUNK)} loaded from cache")
        z.pop("key", None)
        parts.append(z)
    return {k: np.concatenate([p[k] for p in parts], axis=0) for k in parts[0]}


# ------------------------------------------------------------------ labelled rows (development)
SUBS = ["healthy", "noise", "event_scenario_normal", "strong", "faulty_impact", "faulty_shift"]


def flat_rows(F, comm, label, sub, bidx0=0):
    nb, nf, nw, _ = F.shape
    b = np.broadcast_to(np.arange(nb)[:, None, None], (nb, nf, nw))
    fl = np.broadcast_to(np.arange(nf)[None, :, None], (nb, nf, nw))
    return {"F": F.reshape(-1, 6), "comm": np.broadcast_to(comm[:, :, None, :], (nb, nf, nw, 2)).reshape(-1, 2), "b": b.reshape(-1) + bidx0,
            "floor": fl.reshape(-1) + 1, "label": label.reshape(-1).astype(int), "sub": np.full(F.size // 6, sub, dtype=int)}


def dev_pool(D, spec):
    """All labelled windows of the development buildings (before class balancing)."""
    comm = D["comm"]
    nb, nw = D["F_healthy"].shape[0], D["F_healthy"].shape[2]
    parts = [flat_rows(D["F_healthy"], comm, np.zeros((nb, NF, nw)), 0), flat_rows(D["F_noise"], comm, np.zeros((nb, NF, nw)), 1)]
    for nm in ("s3", "s5"):
        parts.append(flat_rows(D["F_" + nm], comm, np.ones((nb, NF, nw)), 3))
    for kind, code in (("imp", 4), ("shift", 5)):
        for k in range(spec["n_" + kind]):
            lab = np.zeros((nb, NF, nw))
            fl, mk = D[f"floor_{kind}{k}"], D[f"mask_{kind}{k}"]
            for i in range(nb):
                lab[i, fl[i] - 1, mk[i]] = 2
            r = flat_rows(D[f"F_{kind}{k}"], comm, lab, 2)
            r["sub"] = np.where(r["label"] == 2, code, 2)
            parts.append(r)
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}


def balance(pool, seed):
    """Class-balanced training rows: every faulty window; equal numbers of abnormal (uniform over the strong pool) and
    normal (one third each: healthy ambient, noise-only, non-event windows of event scenarios). No class weights."""
    rng = np.random.default_rng(seed)
    lab, sub = pool["label"], pool["sub"]
    f_idx = np.flatnonzero(lab == 2)
    nf = len(f_idx)
    ab = rng.choice(np.flatnonzero(lab == 1), nf, replace=False)
    third = nf // 3
    nrm = [rng.choice(np.flatnonzero(sub == s), third + (nf - 3 * third if s == 0 else 0), replace=False) for s in (0, 1, 2)]
    idx = np.sort(np.concatenate([f_idx, ab] + nrm))
    return {k: v[idx] for k, v in pool.items()}


# ------------------------------------------------------------------ models (train.py grid, footprint, voting)
def grid(small):
    cfgs = [{"name": f"dt_d{d}", "family": "tree", "params": {"max_depth": d}} for d in ([3, 4] if small else [3, 4, 5, 6, 7, 8])]
    for n in ([5] if small else [5, 10, 20, 50]):
        for d in ([4] if small else [4, 6, 8]):
            cfgs.append({"name": f"rf_n{n}_d{d}", "family": "forest", "params": {"n_estimators": n, "max_depth": d}})
    return cfgs


def make(cfg):
    p = cfg["params"]
    if cfg["family"] == "tree":
        return DecisionTreeClassifier(max_depth=p["max_depth"], random_state=SEED)
    return RandomForestClassifier(n_estimators=p["n_estimators"], max_depth=p["max_depth"], random_state=SEED, n_jobs=1)


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
    return np.argmax(rf_votes(model, X), axis=1) if family == "forest" else model.predict(X).astype(int)


def footprint(cfg, model):
    """train.py footprint(): 10 bytes per node (feature idx u8 + threshold f32 + left i16 + right i16 + class u8); forest = sum."""
    trees = [model.tree_] if cfg["family"] == "tree" else [e.tree_ for e in model.estimators_]
    nodes = sum(t.node_count for t in trees)
    return {"nodes": int(nodes), "deployed_bytes": int(10 * nodes), "max_comparisons": int(sum(t.max_depth for t in trees))}


def cv_folds(n_buildings, repeats):
    """fold id per building, per repeat (grouped by building)."""
    out = []
    for r in range(repeats):
        perm = np.random.default_rng(SEED + 1 + r).permutation(n_buildings)
        f = np.empty(n_buildings, dtype=int)
        f[perm] = np.arange(n_buildings) % 5
        out.append(f)
    return out


def run_cv(cfg, X, y, bld, folds):
    accs = np.zeros((len(folds), 5))
    for r, fold_of_b in enumerate(folds):
        fo = fold_of_b[bld]
        for k in range(5):
            te = fo == k
            assert not set(bld[te]) & set(bld[~te])  # grouped by building, never by window
            m = make(cfg).fit(X[~te], y[~te])
            accs[r, k] = (predict(cfg["family"], m, X[te]) == y[te]).mean()
    return accs


def select_for_set(set_name, names, pool_bal, cfgs, repeats, cache_dir):
    X = make_X(pool_bal["F"], pool_bal["comm"], names)
    y, bld = pool_bal["label"], pool_bal["b"]
    folds = cv_folds(int(bld.max()) + 1, repeats)
    dkey = hashlib.sha256(X.tobytes() + y.tobytes() + bld.tobytes() + json.dumps(names).encode()).hexdigest()
    results = []
    for cfg in cfgs:
        full = make(cfg).fit(X, y)
        fp = footprint(cfg, full)
        res = {"name": cfg["name"], "family": cfg["family"], "params": cfg["params"], "footprint": fp, "feature_set": set_name}
        if fp["deployed_bytes"] > MAX_BYTES:
            res.update({"cv": None, "eligible": False, "cv_skipped": f"footprint {fp['deployed_bytes']} B > {MAX_BYTES} B: cannot be eligible, CV not run"})
            log(f"  [{set_name}] {cfg['name']:10s} {fp['deployed_bytes']:6d} B > {MAX_BYTES} B -> ineligible (CV skipped)")
        else:
            ck = hashlib.sha256(json.dumps({"d": dkey, "cfg": cfg, "rep": repeats, "seed": SEED}, sort_keys=True).encode()).hexdigest()[:20]
            cf = cache_dir / f"cv_{set_name}_{cfg['name']}_{ck}.json"
            if cf.exists():
                accs = np.array(json.loads(cf.read_text())["accs"])
            else:
                t = time.time()
                accs = run_cv(cfg, X, y, bld, folds)
                cf.write_text(json.dumps({"accs": accs.tolist()}))
                log(f"  [{set_name}] {cfg['name']:10s} CV done in {time.time() - t:.0f}s")
            rm = accs.mean(axis=1)
            res["cv"] = {"mean": float(rm.mean()), "sd": float(rm.std(ddof=1)) if len(rm) > 1 else 0.0, "min": float(rm.min()), "max": float(rm.max()),
                         "repeats_ge_0.90": int((rm >= THRESH).sum()), "repeat_means": rm.tolist(), "fold_acc_min": float(accs.min())}
            res["eligible"] = bool(fp["deployed_bytes"] <= MAX_BYTES and (rm >= THRESH).all())
            log(f"  [{set_name}] {cfg['name']:10s} cv mean {rm.mean():.4f} min {rm.min():.4f} >=0.90 {int((rm >= THRESH).sum())}/{len(rm)} {fp['deployed_bytes']:6d} B eligible {res['eligible']}")
        results.append(res)
    elig = [r for r in results if r["eligible"]]
    winner = min(elig, key=lambda r: (r["footprint"]["deployed_bytes"], -r["cv"]["mean"]))["name"] if elig else None
    return {"feature_set": set_name, "features": names, "configs": results, "winner": winner, "any_eligible": winner is not None,
            "n_rows": int(len(y)), "n_buildings": int(bld.max()) + 1}


def pick_overall(per_set):
    """Smaller footprint across sets that have an eligible config (tie: Set A). If none qualifies: best CV mean."""
    cands = []
    for s in ("A", "B"):
        sel = per_set[s]
        if sel["winner"]:
            r = next(c for c in sel["configs"] if c["name"] == sel["winner"])
            cands.append((r["footprint"]["deployed_bytes"], 0 if s == "A" else 1, s, r))
    if cands:
        fp, _, s, r = min(cands, key=lambda c: (c[0], c[1]))
        return s, r, True
    best = []
    for s in ("A", "B"):
        for r in per_set[s]["configs"]:
            if r["cv"] is not None:
                best.append((-r["cv"]["mean"], r["footprint"]["deployed_bytes"], 0 if s == "A" else 1, s, r))
    if best:
        _, _, _, s, r = min(best, key=lambda c: c[:3])
        return s, r, False
    r = min((c for s in ("A", "B") for c in per_set[s]["configs"]), key=lambda c: c["footprint"]["deployed_bytes"])
    return r["feature_set"], r, False


# ------------------------------------------------------------------ held-out evaluation
class Predictor:
    def __init__(self, name, fn):
        self.name, self.fn = name, fn

    def __call__(self, F, comm):
        """F (nb, 10, nw, 6), comm (nb, 10, 1, 2) -> class (nb, 10, nw)"""
        return self.fn(F, comm)


def old_predictor(tree, gate=None):
    def fn(F, comm):
        f = {"az_std": F[..., 0], "az_kurt": F[..., 1], "az_domfreq": F[..., 2], "gx_std": F[..., 3], "gx_domfreq": F[..., 5]}
        p = tree.predict_model_h(f)
        return np.where(F[..., 0] < gate, 0, p) if gate is not None else p
    return fn


def model_predictor(cfg, model, names, cols=None):
    def fn(F, comm):
        X = make_X(F, comm, names)
        return predict(cfg["family"], model, X if cols is None else X[:, cols]).reshape(F.shape[:-1])
    return fn


def smooth(pred):
    sh = pred.shape
    return lg.smooth_sequences(pred.reshape(-1, sh[-1])).reshape(sh)


def evaluate(H, pred_fn, nw_label):
    """Per-building statistics for one predictor on the held-out buildings (bootstrap is done on these by building)."""
    nb = H["F_healthy"].shape[0]
    comm = H["comm"][:, :, None, :]
    keys = [k for k in H if k.startswith("F_")]
    raw = {k[2:]: pred_fn(H[k], comm) for k in keys}
    sm = {k: smooth(v) for k, v in raw.items()}
    st = {"nb": nb}
    # false alarm: healthy + noise-only, per building and floor, share non-normal
    st["fa"] = np.stack([(sm["healthy"] != 0).mean(axis=2), (sm["noise"] != 0).mean(axis=2)]).mean(axis=0)
    st["fa_raw"] = np.stack([(raw["healthy"] != 0).mean(axis=2), (raw["noise"] != 0).mean(axis=2)]).mean(axis=0)
    st["fa_healthy_only"] = (sm["healthy"] != 0).mean(axis=2)
    st["fa_noise_only"] = (sm["noise"] != 0).mean(axis=2)
    # local events
    for kind in ("imp", "shift"):
        d_arr, n_arr, r_arr, o_arr, on_arr = (np.zeros(nb) for _ in range(5))
        for k in [kk[2:] for kk in keys if kk[2:].startswith(kind)]:
            fl, mk = H["floor_" + k], H["mask_" + k]
            for i in range(nb):
                ev = mk[i]
                n_arr[i] += ev.sum()
                d_arr[i] += (sm[k][i, fl[i] - 1][ev] == 2).sum()
                r_arr[i] += (raw[k][i, fl[i] - 1][ev] == 2).sum()
                oth = np.delete(sm[k][i], fl[i] - 1, axis=0)
                o_arr[i] += (oth == 2).sum()
                on_arr[i] += oth.size
        st[f"det_{kind}"], st[f"det_raw_{kind}"], st[f"n_ev_{kind}"] = d_arr, r_arr, n_arr
        st[f"false_faulty_other_floors_{kind}"], st[f"n_other_{kind}"] = o_arr, on_arr
    st["det"] = st["det_imp"] + st["det_shift"]
    st["det_raw"] = st["det_raw_imp"] + st["det_raw_shift"]
    st["n_ev"] = st["n_ev_imp"] + st["n_ev_shift"]
    # strong events: share abnormal / faulty by floor (3x, 5x) and class mix for 2x and stiffness loss
    for s in ("s3", "s5"):
        for nm, d in (("raw", raw), ("sm", sm)):
            st[f"{s}_{nm}_abnormal_by_floor"] = (d[s] == 1).mean(axis=(0, 2))
            st[f"{s}_{nm}_faulty_by_floor"] = (d[s] == 2).mean(axis=(0, 2))
            st[f"{s}_{nm}_normal_by_floor"] = (d[s] == 0).mean(axis=(0, 2))
    for s in ("s2", "dmg"):
        if s in raw:
            for nm, d in (("raw", raw), ("sm", sm)):
                st[f"{s}_{nm}_class_by_floor"] = np.stack([(d[s] == c).mean(axis=(0, 2)) for c in range(3)], axis=1)
                st[f"{s}_{nm}_class_overall"] = [float((d[s] == c).mean()) for c in range(3)]
    return st


def pooled_ratio(num, den, idx):
    return num[idx].sum(axis=-1) / np.maximum(den[idx].sum(axis=-1), 1)


def bootstrap(st_new, st_old, seed=SEED):
    nb = st_new["nb"]
    idx = np.random.default_rng(seed).integers(0, nb, (N_BOOT, nb))
    out = {}
    fa_n, fa_o = st_new["fa"][idx].mean(axis=(1, 2)), st_old["fa"][idx].mean(axis=(1, 2))
    de_n, de_o = pooled_ratio(st_new["det"], st_new["n_ev"], idx), pooled_ratio(st_old["det"], st_old["n_ev"], idx)
    ci = lambda a: [float(np.percentile(a, 2.5)), float(np.percentile(a, 97.5))]
    out["false_alarm_smoothed"] = {"new": ci(fa_n), "old_gated": ci(fa_o), "diff_new_minus_old": ci(fa_n - fa_o), "prob_new_lower": float((fa_n < fa_o).mean())}
    out["detection_smoothed"] = {"new": ci(de_n), "old_gated": ci(de_o), "diff_new_minus_old": ci(de_n - de_o), "prob_new_ge": float((de_n >= de_o).mean())}
    return out


def headline(st):
    n = st["n_ev"].sum()
    return {
        "false_alarm_smoothed": float(st["fa"].mean()), "false_alarm_raw": float(st["fa_raw"].mean()),
        "false_alarm_smoothed_by_floor": st["fa"].mean(axis=0).tolist(), "false_alarm_raw_by_floor": st["fa_raw"].mean(axis=0).tolist(),
        "false_alarm_smoothed_healthy_only": float(st["fa_healthy_only"].mean()), "false_alarm_smoothed_noise_only": float(st["fa_noise_only"].mean()),
        "detection_smoothed": float(st["det"].sum() / max(n, 1)), "detection_raw": float(st["det_raw"].sum() / max(n, 1)),
        "detection_smoothed_impact": float(st["det_imp"].sum() / max(st["n_ev_imp"].sum(), 1)), "detection_raw_impact": float(st["det_raw_imp"].sum() / max(st["n_ev_imp"].sum(), 1)),
        "detection_smoothed_shift": float(st["det_shift"].sum() / max(st["n_ev_shift"].sum(), 1)), "detection_raw_shift": float(st["det_raw_shift"].sum() / max(st["n_ev_shift"].sum(), 1)),
        "n_event_windows": {"impact": int(st["n_ev_imp"].sum()), "shift": int(st["n_ev_shift"].sum())},
        "false_faulty_other_floors_during_event_smoothed": {k: float(st[f"false_faulty_other_floors_{k}"].sum() / max(st[f"n_other_{k}"].sum(), 1)) for k in ("imp", "shift")},
        "strong_event": {s: {nm: {c: st[f"{s}_{nm}_{c}_by_floor"].tolist() for c in ("normal", "abnormal", "faulty")} for nm in ("raw", "sm")} for s in ("s3", "s5")},
        "level_2x": {nm: {"overall": st.get(f"s2_{nm}_class_overall"), "by_floor": st[f"s2_{nm}_class_by_floor"].tolist()} for nm in ("raw", "sm") if f"s2_{nm}_class_by_floor" in st},
        "stiffness_loss_30pct": {nm: {"overall": st.get(f"dmg_{nm}_class_overall"), "by_floor": st[f"dmg_{nm}_class_by_floor"].tolist()} for nm in ("raw", "sm") if f"dmg_{nm}_class_by_floor" in st},
    }


# ------------------------------------------------------------------ export
def export_model(cfg, model, names):
    fam = cfg["family"]
    export = {"model_type": {"tree": "decision_tree", "forest": "random_forest"}[fam], "config": cfg["name"], "params": cfg["params"],
              "class_order": CLASSES, "input_features": names, "n_input_features": len(names),
              "comparison_rule": "go left if x[feature] <= threshold else right; thresholds are float32 (rounded down so float32 x <= threshold matches sklearn exactly); inputs cast to float32"}
    ests = [model] if fam == "tree" else list(model.estimators_)
    trees = []
    for e in ests:
        t = e.tree_
        thr = t.threshold.astype(np.float32)
        fix = thr.astype(np.float64) > t.threshold
        thr[fix] = np.nextafter(thr[fix], np.float32(-np.inf))
        leaf = t.children_left == -1
        lc = leaf_classes(t, e)
        feat = np.where(leaf, -1, t.feature)
        trees.append({"n_nodes": int(t.node_count), "feature_index": feat.tolist(), "feature_name": [None if f < 0 else names[f] for f in feat],
                      "threshold": [None if lf else float(v) for lf, v in zip(leaf, thr)], "left": np.where(leaf, -1, t.children_left).tolist(),
                      "right": np.where(leaf, -1, t.children_right).tolist(), "leaf_class": [int(c) if lf else None for lf, c in zip(leaf, lc)]})
    export["vote_rule"] = "each tree outputs its leaf class; majority vote over trees; ties -> lowest class index" if fam == "forest" else "n/a (single tree: output the leaf class)"
    export["trees"] = trees
    used_idx = sorted({f for tr in trees for f in tr["feature_index"] if f >= 0})
    used = [names[i] for i in used_idx]
    axes = [a for a in ("ax", "ay", "az", "gx", "gy", "gz") if any(n.startswith(a + "_") for n in used)]
    needed = {a: [n[len(a) + 1:] for n in used if n.startswith(a + "_")] for a in axes}
    export.update({"used_feature_indices": used_idx, "used_feature_names": used, "firmware_must_compute": needed, "firmware_axes_needed": axes,
                   "firmware_needs_fft_axes": [a for a in axes if "domfreq" in needed[a]],
                   "commissioning_values_needed": [n for n in used if n.endswith("_rel")]})
    return export, trees


def np_eval(trees, X):
    X32 = X.astype(np.float32)
    votes = np.zeros((len(X), 3), dtype=int)
    for tr in trees:
        feat, left, right = np.array(tr["feature_index"]), np.array(tr["left"]), np.array(tr["right"])
        thr = np.array([0.0 if v is None else v for v in tr["threshold"]], dtype=np.float32)
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


# ------------------------------------------------------------------ main pipeline
def check_feature_code():
    df = pd.read_csv(ROOT / "data" / "processed" / "dataset.csv")
    idx = np.arange(0, len(df), 11)
    f = feature6(df[[f"az_{i}" for i in range(N)]].to_numpy()[idx], df[[f"gx_{i}" for i in range(N)]].to_numpy()[idx])
    for j, nm in enumerate(BASE_FEATS):
        assert np.allclose(f[:, j], df[nm].to_numpy()[idx], rtol=1e-9, atol=1e-9), nm
    return df


def run(small=False, out_dir=OUT_DIR, cache_dir=None, repeats=None):
    t0 = time.time()
    cache_dir = Path(cache_dir) if cache_dir else SCRATCH / ("cache_small" if small else "cache")
    repeats = repeats or (2 if small else 10)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prior = assert_seed_blocks_disjoint()
    log(f"== seed blocks disjoint from all earlier gate blocks (earlier max {prior}); small={small}, repeats={repeats}")
    ds_csv = check_feature_code()
    tree = lg.Tree()
    G = json.loads((ROOT / "models/selected/validation/localisation_gate.json").read_text())["part_C"]["recommended_G"]
    assert abs(G - 92.12) < 0.01, G
    dev_name, held_name = ("small_dev", "small_held") if small else ("dev", "held")
    held_spec = SMALL_HELD_SPEC if small else HELD_SPEC
    dev_seeds, held_seeds = seeds_of(dev_name), seeds_of(held_name)
    assert not set(dev_seeds) & set(held_seeds) and min(dev_seeds + held_seeds) > 4_000_000
    log("== simulating development buildings"); D = load_block(dev_seeds, DEV_SPEC, cache_dir, dev_name)
    log("== simulating held-out buildings"); H = load_block(held_seeds, held_spec, cache_dir, held_name)

    pool = dev_pool(D, DEV_SPEC)
    bal = balance(pool, SEED + 99)
    sizes = {"development_buildings": len(dev_seeds), "held_out_buildings": len(held_seeds), "dev_pool_windows": int(len(pool["label"])),
             "dev_pool_by_class": {CLASSES[c]: int((pool["label"] == c).sum()) for c in range(3)},
             "dev_pool_by_subtype": {SUBS[s]: int((pool["sub"] == s).sum()) for s in range(6)},
             "dev_training_windows_balanced": int(len(bal["label"])), "dev_training_by_class": {CLASSES[c]: int((bal["label"] == c).sum()) for c in range(3)},
             "dev_training_by_subtype": {SUBS[s]: int((bal["sub"] == s).sum()) for s in range(6)},
             "faulty_windows_per_floor_dev_training": np.bincount(bal["floor"][bal["label"] == 2], minlength=11)[1:].tolist(),
             "class_weights": "none (classes balanced by construction: all faulty windows kept, equal numbers of abnormal and normal sampled)",
             "held_out_windows_per_recording": held_spec["nw"], "dev_windows_per_recording": DEV_SPEC["nw"]}
    log("dataset sizes:", json.dumps(sizes))

    cfgs = grid(small)
    per_set = {}
    for s in ("A", "B"):
        log(f"== selection, feature set {s}: {SETS[s]}")
        per_set[s] = select_for_set(s, SETS[s], bal, cfgs, repeats, cache_dir)
    set_name, chosen, qualified = pick_overall(per_set)
    cfg = next(c for c in cfgs if c["name"] == chosen["name"])
    names = SETS[set_name]
    log(f"== chosen: set {set_name} {cfg['name']} (qualified={qualified}) footprint {chosen['footprint']}")

    Xd, yd = make_X(bal["F"], bal["comm"], names), bal["label"]
    model = make(cfg).fit(Xd, yd)
    export, trees = export_model(cfg, model, names)
    assert np.array_equal(np_eval(trees, Xd), predict(cfg["family"], model, Xd))
    dep_fp = footprint(cfg, model)
    used = export["used_feature_names"]

    log("== held-out evaluation")
    preds = {"new": model_predictor(cfg, model, names), "old_gated": old_predictor(tree, G), "old_ungated": old_predictor(tree, None)}
    stats = {k: evaluate(H, fn, held_spec["nw"]) for k, fn in preds.items()}
    heads = {k: headline(v) for k, v in stats.items()}
    boot = bootstrap(stats["new"], stats["old_gated"])
    new_fa, old_fa = heads["new"]["false_alarm_smoothed"], heads["old_gated"]["false_alarm_smoothed"]
    new_det, old_det = heads["new"]["detection_smoothed"], heads["old_gated"]["detection_smoothed"]
    replace = bool(new_fa < old_fa and new_det >= old_det)
    verdict = "RETRAIN: REPLACE" if replace else "RETRAIN: KEEP OLD"

    # az-only variant of the winning configuration (reported only)
    az_cols = [i for i, n in enumerate(names) if n.startswith("az_")]
    az_names = [names[i] for i in az_cols]
    az_cfg_model = make(cfg).fit(Xd[:, az_cols], yd)
    folds = cv_folds(int(bal["b"].max()) + 1, repeats)
    az_accs = run_cv(cfg, Xd[:, az_cols], yd, bal["b"], folds)
    az_stats = evaluate(H, model_predictor(cfg, az_cfg_model, names, az_cols), held_spec["nw"])
    az_only = {"features": az_names, "footprint": footprint(cfg, az_cfg_model), "dev_cv_mean": float(az_accs.mean(axis=1).mean()), "dev_cv_min_repeat": float(az_accs.mean(axis=1).min()),
               "held_out": {k: v for k, v in headline(az_stats).items() if k in ("false_alarm_smoothed", "false_alarm_raw", "detection_smoothed", "detection_raw", "detection_smoothed_impact", "detection_smoothed_shift")}}

    # dataset.csv (6000 windows) with the new tree
    ycsv = ds_csv["label"].map({c: i for i, c in enumerate(CLASSES)}).to_numpy()
    Fcsv = np.stack([ds_csv[n].to_numpy() for n in BASE_FEATS], axis=-1)
    csv_res = {"windows": int(len(ycsv))}
    for nm, fn in (("old_ungated", preds["old_ungated"]), ("old_gated", preds["old_gated"])):
        p = fn(Fcsv[:, None, None, :], np.ones((len(ycsv), 1, 1, 2))).reshape(-1)
        csv_res[nm] = {"accuracy": float((p == ycsv).mean()), "confusion_true_rows_pred_cols": np.bincount(3 * ycsv + p, minlength=9).reshape(3, 3).tolist()}
    if not any(n.endswith("_rel") for n in names):
        p = predict(cfg["family"], model, make_X(Fcsv, np.ones((len(ycsv), 2)), names))
        csv_res["new"] = {"accuracy": float((p == ycsv).mean()), "confusion_true_rows_pred_cols": np.bincount(3 * ycsv + p, minlength=9).reshape(3, 3).tolist(),
                          "note": "dataset.csv holds isolated synthetic recordings (not buildings); the new tree was not trained on them"}
    else:
        csv_res["new"] = "not computed: the selected tree uses commissioning-relative features, which dataset.csv (isolated recordings) does not have"

    new_only_feats = [n for n in used if n not in CURRENT_FIVE]
    device = {"features_used_by_new_tree": used, "features_outside_current_five": new_only_feats,
              "needs_commissioning_value": bool(any(n.endswith("_rel") for n in used)),
              "commissioning_values_needed": export["commissioning_values_needed"],
              "commissioning_definition": "per node: median az_std (and gx_std) over a separate healthy recording of >= 20 windows, stored on the device (2 floats)",
              "footprint_bytes": dep_fp["deployed_bytes"], "nodes": dep_fp["nodes"]}
    selection_json = {s: {k: v for k, v in per_set[s].items()} for s in per_set}

    report = {
        "task": "T1b retrain on simulated-building data", "small_mode": small, "verdict": verdict, "replaced_old_model": replace,
        "replacement_rule": "new tree replaces the old only if its decisive false alarm rate (mean over floors, healthy ambient + noise-only windows, after 2-of-3 smoothing) is lower than the old tree+gate's AND its local-event detection (event-floor event windows reported faulty after smoothing) is >= the old tree+gate's; no margin",
        "note_status": "models/selected is untouched. models/selected_building holds the retrained model for inspection; deploying it is a separate later step",
        "seeds": {"dev": BLOCKS[dev_name], "held": BLOCKS[held_name], "earlier_blocks_max_seed": prior, "disjoint_asserted": True},
        "scenario": {"dev": DEV_SPEC, "held": held_spec, "exc_ids": EXC, "strong_multipliers": STRONG, "damage_loss": DAMAGE_LOSS, "ambient_level_mult": "U(0.5,1.5) per recording (healthy, impact, shift recordings)",
                     "impact_envelope_cutoff_counts": bs.IMPACT_ENVELOPE_CUTOFF, "old_noise_gate_G": G, "cv_repeats": repeats},
        "dataset_sizes": sizes, "selection": selection_json, "chosen": {"feature_set": set_name, "config": cfg["name"], "qualified": qualified, "dev_footprint": chosen["footprint"], "dev_cv": chosen["cv"], "deployed_footprint": dep_fp},
        "decision": {"new": heads["new"], "old_with_gate": heads["old_gated"], "old_without_gate": heads["old_ungated"], "bootstrap_95ci_by_building": boot, "n_bootstrap": N_BOOT,
                     "decisive": {"false_alarm_new": new_fa, "false_alarm_old_gated": old_fa, "detection_new": new_det, "detection_old_gated": old_det}},
        "az_only_variant": az_only, "dataset_csv_scoring": csv_res, "device_requirements": device,
        "limits": ["Results describe fit to the simulator and say nothing about real structures.", "gx is modelled, not physical (gain x lateral signal + noise + offset).",
                   "The first-mode range 4-10 Hz is a scale model's.", "Excitation is stationary band-limited noise."],
        "limits_of_selection": "RF configurations whose full-development fit exceeds 5120 B were not cross-validated (cannot be eligible by the rule); they are listed with their footprint.",
    }
    jdump(report, out_dir / "report.json")
    joblib.dump(model, out_dir / "model.joblib")
    assert np.array_equal(predict(cfg["family"], joblib.load(out_dir / "model.joblib"), Xd), predict(cfg["family"], model, Xd))
    jdump(export, out_dir / "model_export.json")
    spec = {"fs_hz": FS, "window_n": N, "classes": CLASSES, "input_units": "raw int16 counts as read from MPU6050", "input_columns": names, "used_feature_columns": used,
            "firmware_must_compute": export["firmware_must_compute"],
            "formulas": {"std": "sqrt(mean((x-mean(x))^2)), population (ddof=0), mean removed", "kurt": "excess kurtosis: mean((x-mu)^4)/var^2 - 3 with population moments; 0 if var==0",
                         "domfreq": "(argmax over k=1..64 of |rfft(x-mean(x))[k]|) * FS / N (bin 0 excluded; first max wins ties)",
                         "std_rel": "std of the window divided by that node's commissioning level (median std over >= 20 healthy commissioning windows of the same axis)"},
            "commissioning_values_needed": export["commissioning_values_needed"], "model": {"name": cfg["name"], "family": cfg["family"], "file": "model.joblib", "export_file": "model_export.json",
                                                                                          "trained_on": f"{len(dev_seeds)} simulated development buildings (class-balanced windows)"},
            "not_deployed": True}
    jdump(spec, out_dir / "feature_spec.json")
    (out_dir / "report.md").write_text(render_md(report))
    log(f"wrote {out_dir}; runtime {time.time() - t0:.0f}s")
    log(verdict)
    return report


# ------------------------------------------------------------------ report.md
def f3(x):
    return f"{x:.3f}"


def render_md(R):
    d = R["decision"]
    b = d["bootstrap_95ci_by_building"]
    ci = lambda c: f"[{c[0]:.3f}, {c[1]:.3f}]"
    md = ["# T1b: retraining on simulated-building data", "", f"**{R['verdict']}**" + ("  (small test run, not the real result)" if R["small_mode"] else ""), "",
          "## Limits", ""] + [f"- {x}" for x in R["limits"]] + [
          "- The decision rule and design are fixed in `models/training/train_building.py`; this is not described as pre-registered.", "",
          "## Replacement rule", "", R["replacement_rule"], "", "## Data", ""]
    s = R["dataset_sizes"]
    md += [f"- development buildings {s['development_buildings']}, held-out buildings {s['held_out_buildings']} (seeds {R['seeds']['dev']}, {R['seeds']['held']}; all above 4,000,000 and disjoint from earlier gate blocks, asserted)",
           f"- dev pool windows {s['dev_pool_windows']}: {s['dev_pool_by_class']}; by subtype {s['dev_pool_by_subtype']}",
           f"- balanced training windows {s['dev_training_windows_balanced']}: {s['dev_training_by_class']}; by subtype {s['dev_training_by_subtype']}",
           f"- class weights: {s['class_weights']}", "", "## Selection (development buildings only, grouped repeated 5-fold)", ""]
    for k, sel in R["selection"].items():
        md += [f"### Feature set {k}: {', '.join(sel['features'])}", "", "| config | nodes | bytes | cv mean | min repeat | repeats>=0.90 | eligible |", "|---|---|---|---|---|---|---|"]
        for c in sel["configs"]:
            cv = c["cv"]
            md.append(f"| {c['name']} | {c['footprint']['nodes']} | {c['footprint']['deployed_bytes']} | " + (f"{cv['mean']:.4f} | {cv['min']:.4f} | {cv['repeats_ge_0.90']} | " if cv else "skipped (over 5120 B) | - | - | ") + f"{c['eligible']} |")
        md += ["", f"winner of set {k}: {sel['winner']}", ""]
    ch = R["chosen"]
    md += [f"Chosen: set {ch['feature_set']}, {ch['config']} (a config qualified: {ch['qualified']}); deployed footprint {ch['deployed_footprint']}.", "", "## Decision (held-out buildings)", "",
           "| model | false alarm smoothed | false alarm raw | detection smoothed | detection raw | det. impact (sm) | det. shift (sm) |", "|---|---|---|---|---|---|---|"]
    for nm, key in (("new", "new"), ("old + gate", "old_with_gate"), ("old, no gate", "old_without_gate")):
        h = d[key]
        md.append(f"| {nm} | {f3(h['false_alarm_smoothed'])} | {f3(h['false_alarm_raw'])} | {f3(h['detection_smoothed'])} | {f3(h['detection_raw'])} | {f3(h['detection_smoothed_impact'])} | {f3(h['detection_smoothed_shift'])} |")
    md += ["", "Bootstrap 95% intervals (resampling held-out buildings):", "",
           f"- false alarm (smoothed): new {ci(b['false_alarm_smoothed']['new'])}, old+gate {ci(b['false_alarm_smoothed']['old_gated'])}, difference new-old {ci(b['false_alarm_smoothed']['diff_new_minus_old'])}",
           f"- detection (smoothed): new {ci(b['detection_smoothed']['new'])}, old+gate {ci(b['detection_smoothed']['old_gated'])}, difference new-old {ci(b['detection_smoothed']['diff_new_minus_old'])}", "",
           "False alarm by floor (smoothed), floors 1..10:", ""]
    for nm, key in (("new", "new"), ("old + gate", "old_with_gate"), ("old, no gate", "old_without_gate")):
        md.append(f"- {nm}: " + " ".join(f"{v:.2f}" for v in d[key]["false_alarm_smoothed_by_floor"]))
    md += ["", "False faulty on non-event floors during a local event (smoothed): " + ", ".join(f"{nm}: {d[key]['false_faulty_other_floors_during_event_smoothed']}" for nm, key in (("new", "new"), ("old+gate", "old_with_gate"), ("old no gate", "old_without_gate"))), "",
           "Strong events, share reported abnormal by floor (smoothed), floors 1..10:", ""]
    for s_ in ("s3", "s5"):
        for nm, key in (("new", "new"), ("old + gate", "old_with_gate"), ("old, no gate", "old_without_gate")):
            md.append(f"- {s_[1]}x {nm}: abnormal " + " ".join(f"{v:.2f}" for v in d[key]["strong_event"][s_]["sm"]["abnormal"]) + " | faulty " + " ".join(f"{v:.2f}" for v in d[key]["strong_event"][s_]["sm"]["faulty"]))
    md += ["", "2x level and 30% stiffness loss (class mix [normal, abnormal, faulty], smoothed, all floors):", ""]
    for nm, key in (("new", "new"), ("old + gate", "old_with_gate"), ("old, no gate", "old_without_gate")):
        md.append(f"- {nm}: 2x {d[key]['level_2x']['sm']['overall']}, stiffness loss {d[key]['stiffness_loss_30pct']['sm']['overall']}")
    az = R["az_only_variant"]
    md += ["", f"## az-only variant of the chosen configuration (reported only)", "", f"features {az['features']}, dev CV mean {az['dev_cv_mean']:.4f}, held-out {az['held_out']}", "",
           "## dataset.csv (6000 isolated-recording windows)", "", f"```\n{json.dumps(R['dataset_csv_scoring'], indent=1)}\n```", "", "## What the model needs on the device", ""]
    dv = R["device_requirements"]
    md += [f"- features used: {dv['features_used_by_new_tree']}", f"- outside the current five: {dv['features_outside_current_five']}", f"- commissioning value needed: {dv['needs_commissioning_value']} ({dv['commissioning_definition']})",
           f"- tree size: {dv['nodes']} nodes, {dv['footprint_bytes']} B", "", f"## Note", "", R["limits_of_selection"], ""]
    return "\n".join(md)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--small", action="store_true", help="tiny deterministic subset for testing; writes under the scratch directory, never models/selected_building")
    ap.add_argument("--out", default=None)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--repeats", type=int, default=None)
    a = ap.parse_args(argv)
    out = Path(a.out) if a.out else (SCRATCH / "small_out" if a.small else OUT_DIR)
    if a.small:
        assert out.resolve() != OUT_DIR.resolve(), "small mode must not write the real output directory"
    r = run(a.small, out, a.cache, a.repeats)
    print(r["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
