"""R2b: final selection round for the master fusion rule, with an EVENT-level criterion, on fresh seeds.

Why a second round: the R2 criterion (AMBIENT recall >= 0.95 per superframe) allows about 126 false flags per hour and pushed the
choice to the smoothed tree variant (BUILDING recall 0.43, isolated impacts 0.43). Per-superframe scoring is the wrong unit for an
alarm system. R2's results stay reported (fusion_eval_r2.json/md); R2's test block (seeds 5,200,000+) was used for R2's single
scoring only and is NOT used here. R2b blocks are fresh: development 6,100,000+, test 6,200,000+ (asserted disjoint from every
earlier block including R2's).

Fixed before any R2b result was looked at:
  candidates  rules a, b, c, d x T {1.5, 2, 3} x tree variant {smoothed, raw-or-smoothed} (b has none) x debounce k {1, 2, 3} (63).
              Debounce: a LOCAL(i)/BUILDING key that differs from the current output must hold k consecutive closed superframes
              before it is output (and an event `begin` emitted); AMBIENT is reported meanwhile (an already-output event class is
              kept while another event class is pending); any non-event decision is output at once.
  data        per building (all from building_sim, slaves on floors 5/10 primary and 3/7 reported):
              healthy monitoring = one 240-window recording (level 0.5-1.5x) + one 60-window noise-only recording; hours =
              (240 + 60) x 1.4 s per building (240 buildings = 28 h per block, 22.4 h of it ambient);
              LOCAL events on slave i's floor, three equally weighted kinds, 30 windows each: dense impacts (as in R2), a sparse
              impact (exactly one impact, start uniform in windows 2..24 of the recording), a frequency shift;
              BUILDING events 2x, 3x, 5x (30 windows, every window), equally weighted;
              reported only: impact / shift on an uninstrumented floor.
  metrics     false onsets per hour = event `begin`s (non-event -> LOCAL/BUILDING output) on healthy+noise monitoring / hours.
              Event recall: the correct class (LOCAL with the correct slave, or BUILDING) is output for >= 1 superframe in the
              event's recording ("any"); also "in span" = within the event windows + 3 superframes (BUILDING: whole recording).
              Also onset latency (first correct output at or after the first event window, in superframes), events with a
              wrong-class event output, recall per kind / level, per-superframe metrics.
  selection   development, primary pair: maximise mean(LOCAL event recall, BUILDING event recall) subject to false onsets <= 1.0/h on
              development healthy monitoring (LOCAL = mean of the three kinds, BUILDING = mean of 2x/3x/5x). Ties: fewer false onsets,
              then simpler (a<b<c<d, smaller k, smoothed before raw, lower T). None eligible: lowest false-onset rate.
              The chosen configuration is scored once on the test block (bootstrap by building).
Usage: python models/training/fusion_eval_r2b.py [--small] [--prime dev|test] [--cache DIR]   (--prime only fills the simulation cache)
Cache: --cache DIR, else $RESONANCE_CACHE_DIR/fusion_eval_r2b/cache, else <repo>/.cache/fusion_eval_r2b/cache.
"""
import argparse
import hashlib
import inspect
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import fusion_eval as fe  # noqa: E402
from fusion_eval import bs, lg, N, FLOORS, PAIRS, T_VALUES, RULES, SF_SECONDS, GATE32, CLS_NAMES  # noqa: E402

OUT_DIR = fe.OUT_DIR
import cachedir  # noqa: E402

SCRATCH = cachedir.script_dir("fusion_eval_r2b")   # cache and --small output; see cachedir.py (--cache / RESONANCE_CACHE_DIR / .cache/)
SEED = 42
NW = 30
HL_NW = 240
NZ_NW = 60
COMM_NW = 20
CHUNK = 20
K_VALUES = (1, 2, 3)
FOPH_MAX = 1.0
BUDGETS = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 60.0)  # 10, 20, 60 are an addition beyond the brief, to show where the tree-based rules start to qualify
N_BOOT = 2000
BLOCKS2 = {"dev": (6_100_000, 240), "test": (6_200_000, 240), "small_dev": (6_900_000, 12), "small_test": (6_950_000, 12)}
EXC = {"comm": 0, "hl": 11, "nz": 12, "s3": 3, "s5": 4, "s2": 5, "uimp": 300, "ushift": 301}
STRONG = {"s2": 2.0, "s3": 3.0, "s5": 5.0}
ETYPES = ["dense_s1", "dense_s2", "sparse_s1", "sparse_s2", "shift_s1", "shift_s2", "x2", "x3", "x5"]
KINDS = {"dense": ["dense_s1", "dense_s2"], "sparse": ["sparse_s1", "sparse_s2"], "shift": ["shift_s1", "shift_s2"],
         "x2": ["x2"], "x3": ["x3"], "x5": ["x5"]}
LOCAL_KINDS = ["dense", "sparse", "shift"]
BUILD_KINDS = ["x2", "x3", "x5"]
OUT_NAMES = fe.OUT_NAMES


def log(*a):
    fe.log(*a)


# ------------------------------------------------------------------ seeds
def assert_seed_blocks_disjoint():
    prior = max(fe.prior_max_seed(), max(a + n for a, n in fe.BLOCKS.values()))
    rng = sorted((a, a + n, k) for k, (a, n) in BLOCKS2.items())
    assert rng[0][0] > 5_000_000 and rng[0][0] >= prior, (rng[0], prior)
    assert all(rng[i][1] <= rng[i + 1][0] for i in range(len(rng) - 1)), "R2b seed blocks overlap"
    return prior


def seeds_of(name):
    a, n = BLOCKS2[name]
    return list(range(a, a + n))


# ------------------------------------------------------------------ simulation
def sparse_spike(seed, exc, floor, T):
    """Exactly one impact: generate.py-style damped sinusoid, start uniform in windows 2..24, amplitude U(1500, 4500)."""
    r = np.random.default_rng([0x51, int(seed), int(exc), int(floor)])
    f0 = r.uniform(14, 30) if r.random() < 0.5 else r.uniform(4, 12)
    tau = r.uniform(0.08, 0.25)
    start = int(r.integers(2 * N, T - 5 * N))
    amp = r.uniform(1500, 4500)
    sign = r.choice([-1, 1])
    ph = r.uniform(0, 2 * np.pi)
    tt = np.arange(T - start) / bs.FS
    sp = np.zeros(T)
    sp[start:] = sign * amp * np.exp(-tt / tau) * np.sin(2 * np.pi * f0 * tt + ph)
    return sp, bs.impact_window_mask(T, [start], [amp], tau)


def rec_names2():
    names = ["hl", "nz", "s2", "s3", "s5", "uimp", "ushift"]
    for pre in ("imp", "sp", "shift"):
        names += [f"{pre}{f}" for f in FLOORS]
    return names


def chunk_key(seeds):
    h = hashlib.sha256()
    h.update(Path(bs.__file__).read_bytes())
    h.update((fe.ROOT / "firmware/esp32/include/model.h").read_bytes())
    h.update(inspect.getsource(lg.window_features).encode())
    h.update(inspect.getsource(simulate_chunk).encode())
    h.update(inspect.getsource(sparse_spike).encode())
    h.update(json.dumps({"seeds": [int(s) for s in seeds], "nw": NW, "hl": HL_NW, "nz": NZ_NW, "comm": COMM_NW, "exc": EXC, "floors": FLOORS,
                         "unf": fe.UNINSTR_FLOORS, "strong": STRONG, "version": 1}, sort_keys=True).encode())
    return h.hexdigest()


def simulate_chunk(seeds):
    tree = lg.Tree()
    ps = [bs.building_params(s) for s in seeds]
    nb = len(ps)
    out = {"seeds": np.array(seeds)}

    def record(exc, nwin, amp=1.0, zero=False, mult=None, kind=None, floors=None, batch=20):
        sig = np.zeros((nb, bs.NFLOOR, nwin * N)) if zero else bs.simulate_many(ps, exc, nwin, None, "default", amp, batch=batch)
        if mult is not None:
            sig = sig * mult[:, None, None]
        std = np.empty((nb, bs.NFLOOR, nwin), dtype=np.float32)
        cls = np.empty((nb, bs.NFLOOR, nwin), dtype=np.uint8)
        mask = np.zeros((nb, nwin), dtype=bool)
        for i, p in enumerate(ps):
            kw = {}
            s_i = sig[i]
            if kind == "imp":
                kw["impact_floor"] = int(floors[i])
            elif kind == "shift":
                kw["shift_floor"] = int(floors[i])
            elif kind == "sp":
                sp, mk = sparse_spike(p["seed"], exc, int(floors[i]), nwin * N)
                s_i = s_i.copy()
                s_i[int(floors[i]) - 1] += sp  # gx follows through gx_gain * floor signal, exactly like add_impact
                mask[i] = mk
            az, gx, tr = bs.sensor_output(p, s_i, exc, return_truth=True, **kw)
            a_std, a_kurt, a_dom = lg.window_features(bs.to_windows(az))
            g_std, _, g_dom = lg.window_features(bs.to_windows(gx))
            std[i] = a_std.astype(np.float32)
            cls[i] = tree.predict_model_h({"az_std": a_std, "az_kurt": a_kurt, "az_domfreq": a_dom, "gx_std": g_std, "gx_domfreq": g_dom})
            m = tr["impact_mask"] if kind == "imp" else tr["shift_mask"] if kind == "shift" else None
            if m is not None:
                mask[i] = m
        return std, cls, mask

    out["comm_std"], _, _ = record(EXC["comm"], COMM_NW)
    mh = np.array([fe.level_mult(p["seed"], EXC["hl"]) for p in ps])
    out["hl_std"], out["hl_cls"], _ = record(EXC["hl"], HL_NW, mult=mh, batch=10)
    out["nz_std"], out["nz_cls"], _ = record(EXC["nz"], NZ_NW, zero=True)
    for nm, a in STRONG.items():
        out[nm + "_std"], out[nm + "_cls"], _ = record(EXC[nm], NW, amp=a)
    for kind, base_exc in (("imp", 100), ("sp", 400), ("shift", 200)):
        for f in FLOORS:
            exc = base_exc + f
            m = np.array([fe.level_mult(p["seed"], exc) for p in ps])
            out[f"{kind}{f}_std"], out[f"{kind}{f}_cls"], out[f"{kind}{f}_mask"] = record(exc, NW, mult=m, kind=kind, floors=np.full(nb, f))
    for kind, nm in (("imp", "uimp"), ("shift", "ushift")):
        m = np.array([fe.level_mult(p["seed"], EXC[nm]) for p in ps])
        fl = np.array([fe.uninstr_floor(p["seed"], kind) for p in ps])
        out[nm + "_std"], out[nm + "_cls"], out[nm + "_mask"] = record(EXC[nm], NW, mult=m, kind=kind, floors=fl)
        out[nm + "_floor"] = fl
    return out


def load_block(seeds, cache_dir, tag):
    cache_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    nchunks = -(-len(seeds) // CHUNK)
    for i in range(0, len(seeds), CHUNK):
        sd = seeds[i:i + CHUNK]
        key = chunk_key(sd)
        f = cache_dir / f"r2b_{tag}_{sd[0]}_{len(sd)}_{key[:16]}.npz"
        z = None
        if f.exists():
            try:
                z = dict(np.load(f))
                assert str(z["key"]) == key and np.array_equal(z["seeds"], sd)
            except Exception:  # noqa: BLE001
                z = None
        if z is None:
            t = time.time()
            z = simulate_chunk(sd)
            tmp = f.with_suffix(".tmp.npz")
            np.savez(tmp, key=np.array(key), **z)
            tmp.replace(f)
            log(f"  [{tag}] simulated chunk {i // CHUNK + 1}/{nchunks} ({len(sd)} buildings) in {time.time() - t:.0f}s")
        else:
            log(f"  [{tag}] chunk {i // CHUNK + 1}/{nchunks} loaded from cache")
        z.pop("key", None)
        parts.append(z)
    return {k: np.concatenate([p[k] for p in parts], axis=0) for k in parts[0]}


def derive(D):
    R = {}
    comm = np.sort(D["comm_std"], axis=2)
    n = comm.shape[2]
    base = ((comm[..., n // 2 - 1] + comm[..., n // 2]) * np.float32(0.5)).astype(np.float32)
    for nm in rec_names2():
        std = D[nm + "_std"]
        raw = np.where(std < GATE32, 0, D[nm + "_cls"]).astype(np.int8)
        R[nm] = {"std": std, "raw": raw, "state": fe.smooth_seq(raw).astype(np.int8), "mask": D.get(nm + "_mask")}
    return R, base


class PV:
    """Floors (fa, fb) of a block."""

    def __init__(self, R, base, fa, fb):
        self.fa, self.fb = fa, fb
        sel = [fa - 1, fb - 1]
        self.base = np.maximum(base[:, sel], np.float32(1.0))
        self.rec = {}
        for nm, r in R.items():
            std = r["std"][:, sel]
            self.rec[nm] = {"r": (std / self.base[:, :, None]).astype(np.float32), "raw": r["raw"][:, sel], "state": r["state"][:, sel], "mask": r["mask"]}
        self._e = {}

    def e(self, nm, T):
        k = (nm, T)
        if k not in self._e:
            self._e[k] = fe.smooth_seq((self.rec[nm]["r"] >= np.float32(T)).astype(int)) == 1
        return self._e[k]

    def event_rec(self, et):
        """-> (recording name, truth code, mask (nb, nw) or None = all windows)"""
        if et in ("x2", "x3", "x5"):
            return "s" + et[1], 3, None
        kind, sl = et.split("_")
        f = self.fa if sl == "s1" else self.fb
        pre = {"dense": "imp", "sparse": "sp", "shift": "shift"}[kind]
        return f"{pre}{f}", 1 if sl == "s1" else 2, self.rec[f"{pre}{f}"]["mask"]

    def needed(self):
        names = ["hl", "nz", "uimp", "ushift"]
        for et in ETYPES:
            names.append(self.event_rec(et)[0])
        return names


# ------------------------------------------------------------------ debounce and statistics
def debounce(o, k):
    """Vectorised twin of RefFusion._debounce / Fusion debounce on rule outputs (codes 0 AMBIENT, 1/2 LOCAL(i), 3 BUILDING)."""
    if k <= 1:
        return o
    nb, nw = o.shape
    out = np.zeros(nb, dtype=o.dtype)
    cand = np.zeros(nb, dtype=o.dtype)
    cnt = np.zeros(nb, dtype=int)
    res = np.empty_like(o)
    for t in range(nw):
        d = o[:, t]
        ev = d > 0
        same = d == out
        newc = ev & ~same
        cnt = np.where(newc, np.where(cand == d, cnt + 1, 1), 0)
        cand = np.where(newc, d, 0)
        accept = newc & (cnt >= k)
        out = np.where(~ev, 0, np.where(same | accept, d, out))
        cnt = np.where(accept, 0, cnt)
        cand = np.where(accept, 0, cand)
        res[:, t] = out
    return res


def conf_counts(o, tr):
    nb = o.shape[0]
    c = np.zeros((nb, 4, 4))
    for t in range(4):
        for p in range(4):
            c[:, t, p] = ((tr == t) & (o == p)).sum(1)
    return c


def stats_from(outs, v):
    nb = outs["hl"].shape[0]
    st = {}
    for key, nm in (("healthy", "hl"), ("noise", "nz")):
        o = outs[nm]
        prev = np.concatenate([np.zeros((nb, 1), dtype=o.dtype), o[:, :-1]], axis=1)
        beg = (o > 0) & (prev == 0)
        st[key + "_n"] = np.full(nb, o.shape[1], dtype=float)
        st[key + "_begins"] = beg.sum(1).astype(float)
        st[key + "_begins_local"] = (beg & ((o == 1) | (o == 2))).sum(1).astype(float)
        st[key + "_begins_building"] = (beg & (o == 3)).sum(1).astype(float)
        st[key + "_ambient"] = (o == 0).sum(1).astype(float)
    for et in ETYPES:
        nm, tru, mask = v.event_rec(et)
        o = outs[nm]
        nw = o.shape[1]
        w = np.arange(nw)[None, :]
        m = np.ones((nb, nw), dtype=bool) if mask is None else mask
        valid = m.any(1)
        s0 = np.argmax(m, axis=1)
        last = nw - 1 - np.argmax(m[:, ::-1], axis=1)
        span = (w >= s0[:, None]) & (w <= last[:, None] + 3)
        ok = o == tru
        after = ok & (w >= s0[:, None])
        first = np.where(after.any(1), after.argmax(1), -1)
        st[et + "_valid"] = valid.astype(float)
        st[et + "_det"] = (ok.any(1) & valid).astype(float)
        st[et + "_span"] = ((ok & span).any(1) & valid).astype(float)
        st[et + "_lat"] = np.where((first >= 0) & valid, first - s0, np.nan)
        st[et + "_wrong"] = (((o > 0) & (o != tru)).any(1) & valid).astype(float)
        st[et + "_conf"] = conf_counts(o, np.where(m, tru, 0)) * valid[:, None, None]
        if et.startswith("dense"):
            nbr = np.zeros_like(m)
            nbr[:, 1:] |= m[:, :-1]
            nbr[:, :-1] |= m[:, 1:]
            iso = m & ~nbr
            st[et + "_iso"] = np.stack([((o == tru) & iso).sum(1), iso.sum(1)], 1).astype(float)
    for nm in ("uimp", "ushift"):
        o = outs[nm]
        st[nm + "_has"] = np.stack([(o == c).any(1) for c in (1, 2, 3)], 1).astype(float)
    return st


def run_rule(rcfg, v, ks=K_VALUES):
    outs = {nm: fe.decide_vec(rcfg, v, nm) for nm in v.needed()}
    return {k: stats_from({nm: debounce(o, k) for nm, o in outs.items()}, v) for k in ks}


def all_cfgs():
    cfgs = []
    for rc in fe.configs():
        for k in K_VALUES:
            c = dict(rc, k=k)
            c["name"] = f"{rc['name']}, k={k}"
            cfgs.append(c)
    return cfgs


# ------------------------------------------------------------------ summarising (point estimate or bootstrap)
def _agg(x, W):
    return x.sum(0) if W is None else np.tensordot(W, x, axes=(1, 0))


def _ratio(a, b):
    return np.divide(a, b, out=np.full(np.shape(a), np.nan), where=np.asarray(b) > 0)


def summarize(st, W=None):
    """Metrics from per-building arrays; with W (B, nb) bootstrap weights every metric is an array of B values."""
    A = lambda key: _agg(st[key], W)  # noqa: E731
    m = {}
    hours = (A("healthy_n") + A("noise_n")) * SF_SECONDS / 3600.0
    m["false_onsets_per_hour"] = (A("healthy_begins") + A("noise_begins")) / hours
    m["false_onsets_per_hour_healthy"] = A("healthy_begins") / (A("healthy_n") * SF_SECONDS / 3600.0)
    m["false_onsets_per_hour_noise"] = A("noise_begins") / (A("noise_n") * SF_SECONDS / 3600.0)
    m["false_local_onsets_per_hour"] = (A("healthy_begins_local") + A("noise_begins_local")) / hours
    m["false_building_onsets_per_hour"] = (A("healthy_begins_building") + A("noise_begins_building")) / hours
    m["monitoring_hours"] = hours if W is None else None
    m["ambient_recall_superframe"] = (A("healthy_ambient") + A("noise_ambient")) / (A("healthy_n") + A("noise_n"))
    for which in ("det", "span"):
        for kind, ets in KINDS.items():
            num = sum(A(e + "_" + which) for e in ets)
            den = sum(A(e + "_valid") for e in ets)
            m[f"recall_{which}_{kind}"] = _ratio(num, den)
        m[f"{which}_local_recall"] = sum(m[f"recall_{which}_{k}"] for k in LOCAL_KINDS) / 3
        m[f"{which}_building_recall"] = sum(m[f"recall_{which}_{k}"] for k in BUILD_KINDS) / 3
        m[f"{which}_mean"] = (m[f"{which}_local_recall"] + m[f"{which}_building_recall"]) / 2
    for kind, ets in KINDS.items():
        den = sum(A(e + "_valid") for e in ets)
        m[f"wrong_class_{kind}"] = _ratio(sum(A(e + "_wrong") for e in ets), den)
        latv = np.concatenate([np.nan_to_num(st[e + "_lat"], nan=0.0)[None] for e in ets])
        cntv = np.concatenate([(~np.isnan(st[e + "_lat"]))[None].astype(float) for e in ets])
        m[f"latency_{kind}"] = _ratio(_agg(latv.sum(0), W), _agg(cntv.sum(0), W))
    # per-superframe metrics (R2-style) pooled over the event recordings of the pair
    loc = sum(A(e + "_conf") for e in ETYPES[:6])
    bld = sum(A(e + "_conf") for e in ETYPES[6:])
    m["superframe_local_recall"] = _ratio(loc[..., 1, 1] + loc[..., 2, 2], loc[..., 1, :].sum(-1) + loc[..., 2, :].sum(-1))
    m["superframe_building_recall"] = _ratio(bld[..., 3, 3], bld[..., 3, :].sum(-1))
    m["superframe_wrong_slave"] = _ratio(loc[..., 1, 2] + loc[..., 2, 1], loc[..., 1, :].sum(-1) + loc[..., 2, :].sum(-1))
    # dense isolated windows
    ih = sum(A(e + "_iso")[..., 0] for e in ETYPES[:2])
    it = sum(A(e + "_iso")[..., 1] for e in ETYPES[:2])
    m["isolated_dense_window_recall"] = _ratio(ih, it)
    m["n_isolated_dense_windows"] = it if W is None else None
    return m


def point_row(cfg, st):
    m = summarize(st)
    row = {"name": cfg["name"], "rule": cfg["rule"], "T": cfg["T"], "use_raw": cfg["use_raw"], "k": cfg["k"]}
    for key in ("false_onsets_per_hour", "false_onsets_per_hour_healthy", "false_onsets_per_hour_noise", "ambient_recall_superframe", "det_local_recall",
                "det_building_recall", "det_mean", "span_mean", "recall_det_dense", "recall_det_sparse", "recall_det_shift", "recall_det_x2",
                "recall_det_x3", "recall_det_x5", "latency_dense", "latency_sparse", "latency_shift", "latency_x3", "superframe_local_recall",
                "superframe_building_recall", "isolated_dense_window_recall"):
        row[key] = float(m[key])
    row["wrong_class_local"] = float(np.nanmean([m["wrong_class_dense"], m["wrong_class_sparse"], m["wrong_class_shift"]]))
    row["wrong_class_building"] = float(np.nanmean([m["wrong_class_x2"], m["wrong_class_x3"], m["wrong_class_x5"]]))
    row["eligible"] = bool(row["false_onsets_per_hour"] <= FOPH_MAX)
    return row


def sel_key(r):
    """Larger is better."""
    return (r["det_mean"], -r["false_onsets_per_hour"], -RULES.index(r["rule"]), -r["k"], -int(r["use_raw"]), -r["T"])


def choose(rows, budget=FOPH_MAX):
    elig = [r for r in rows if r["false_onsets_per_hour"] <= budget]
    if elig:
        return max(elig, key=sel_key), True
    return min(rows, key=lambda r: (r["false_onsets_per_hour"],) + tuple(-x for x in sel_key(r))), False


def boot_weights(nb, B):
    return fe.boot_weights(nb, B)


def with_ci(m, keys):
    out = {}
    pt = summarize_cache["pt"]
    for k in keys:
        out[k] = {"value": float(pt[k]), "ci95": fe.ci(m[k])}
    return out


summarize_cache = {}


def full_report(st, nb, B):
    W = boot_weights(nb, B)
    pt = summarize(st)
    bt = summarize(st, W)
    summarize_cache["pt"] = pt
    keys = [k for k in pt if k not in ("monitoring_hours", "n_isolated_dense_windows")]
    rep = {k: {"value": float(pt[k]), "ci95": fe.ci(bt[k])} for k in keys}
    rep["monitoring_hours"] = float(pt["monitoring_hours"])
    rep["n_isolated_dense_windows"] = int(pt["n_isolated_dense_windows"])
    rep["n_events"] = {k: int(sum(st[e + "_valid"].sum() for e in ets)) for k, ets in KINDS.items()}
    rep["n_buildings"] = nb
    # uninstrumented: share of recordings with any / which event class output
    rep["uninstrumented_reported_only"] = {}
    for nm in ("uimp", "ushift"):
        h = st[nm + "_has"]
        anyev = h.max(1)
        rep["uninstrumented_reported_only"][nm] = {
            "any_event_output": {"value": float(anyev.mean()), "ci95": fe.ci((W @ anyev) / W.sum(1))},
            "LOCAL(1)": float(h[:, 0].mean()), "LOCAL(2)": float(h[:, 1].mean()), "BUILDING": float(h[:, 2].mean())}
    return rep


# ------------------------------------------------------------------ reference cross-check
def crosscheck(v, based_nb=2):
    mism = chk = 0
    for rc in fe.configs()[::4]:
        outs = {nm: fe.decide_vec(rc, v, nm) for nm in v.needed()}
        for k in K_VALUES:
            for nm in ("hl", "nz", v.event_rec("dense_s1")[0], v.event_rec("sparse_s2")[0], v.event_rec("shift_s1")[0], "s3"):
                o = debounce(outs[nm], k)
                rec = v.rec[nm]
                for b in range(based_nb):
                    rf = fe.RefFusion(rc["rule"], rc["T"], rc["use_raw"], debounce=k)
                    sf = 0
                    for _ in range(fe.COMM_NW):
                        for i in (0, 1):
                            rf.on_report(i + 1, sf, 0, 0, v.base[b, i], sf)
                        sf += 1
                    n0 = len(rf.decisions)
                    nw = rec["r"].shape[2]
                    for w in range(min(nw, 80)):
                        for i in (0, 1):
                            rf.on_report(i + 1, sf, int(rec["raw"][b, i, w]), int(rec["state"][b, i, w]), v._std[nm][b, i, w], sf)
                        sf += 1
                    for w, d in enumerate(rf.decisions[n0:]):
                        ref = {fe.C_AMBIENT: 0, fe.C_BUILDING: 3}.get(d[1], d[2] if d[1] == fe.C_LOCAL else -1)
                        chk += 1
                        mism += int(ref != o[b, w])
    return {"superframes_checked": chk, "mismatches": mism}


# ------------------------------------------------------------------ run
def run(small=False, cache_dir=None, out_dir=None, write=True, prime=None):
    t0 = time.time()
    cache_dir = cache_dir or SCRATCH / "cache"
    prior = assert_seed_blocks_disjoint()
    dn, tn = ("small_dev", "small_test") if small else ("dev", "test")
    B = 200 if small else N_BOOT
    log(f"== R2b small={small}: blocks {BLOCKS2[dn]}, {BLOCKS2[tn]} (earlier max seed {prior}; disjointness asserted)")
    if prime:
        load_block(seeds_of(dn if prime == "dev" else tn), cache_dir, dn if prime == "dev" else tn)
        return None
    Dd = load_block(seeds_of(dn), cache_dir, dn)
    Rd, based = derive(Dd)
    vd = PV(Rd, based, *PAIRS["primary"])
    vd._std = {nm: Rd[nm]["std"][:, [PAIRS["primary"][0] - 1, PAIRS["primary"][1] - 1]] for nm in Rd}
    log("== development: evaluating 63 candidates")
    rows, rule_cfgs = [], fe.configs()
    for rc in rule_cfgs:
        res = run_rule(rc, vd)
        for k in K_VALUES:
            rows.append(point_row(dict(rc, k=k, name=f"{rc['name']}, k={k}"), res[k]))
    chosen, met = choose(rows)
    tradeoff = {}
    for b in BUDGETS:
        r, ok = choose(rows, b)
        tradeoff[str(b)] = dict(r, budget_met=ok)
    xc = crosscheck(vd)
    log(f"== chosen {chosen['name']} constraint_met={met}; reference check {xc}")
    # ---- test: scored once
    Dt = load_block(seeds_of(tn), cache_dir, tn)
    Rt, baset = derive(Dt)
    rc = next(c for c in rule_cfgs if c["rule"] == chosen["rule"] and c["T"] == chosen["T"] and c["use_raw"] == chosen["use_raw"])
    test = {}
    for pn, (fa, fb) in PAIRS.items():
        vt = PV(Rt, baset, fa, fb)
        st = run_rule(rc, vt, (chosen["k"],))[chosen["k"]]
        test[pn] = dict(full_report(st, vt.base.shape[0], B), floors=[fa, fb])
    # reference only (not a selection): the R2 configuration (d, T=3, smoothed, k=1) on the R2b test block, primary pair
    r2c = next(c for c in rule_cfgs if c["rule"] == "d" and c["T"] == 3.0 and not c["use_raw"])
    vt = PV(Rt, baset, *PAIRS["primary"])
    ref_r2 = full_report(run_rule(r2c, vt, (1,))[1], vt.base.shape[0], B)
    out = {
        "round": "R2b", "small_mode": small,
        "seed_blocks": {k: {"start": a, "n": n} for k, (a, n) in BLOCKS2.items() if k in (dn, tn)}, "earlier_max_seed": prior, "seed_disjointness_asserted": True,
        "setup": {"healthy_windows": HL_NW, "noise_windows": NZ_NW, "event_windows": NW, "commissioning_windows": COMM_NW, "K": K_VALUES, "T_values": T_VALUES,
                  "false_onset_budget_per_hour": FOPH_MAX, "bootstrap_resamples": B, "pairs": PAIRS, "sparse_impact_start": "uniform in windows 2..24"},
        "development_table_primary_pair": rows, "selected": dict(chosen, constraint_met=met), "tradeoff": tradeoff,
        "reference_vs_vectorised": xc, "test": test, "reference_r2_config_on_r2b_test": ref_r2,
    }
    if write:
        od = out_dir or OUT_DIR
        od.mkdir(parents=True, exist_ok=True)
        fe.jdump(out, od / "fusion_eval_r2b_only.json")
        (od / "fusion_eval_r2b_only.md").write_text(render_md(out), encoding="utf-8")
        merge(od, out)
    log(f"== done in {time.time() - t0:.0f}s")
    return out


# ------------------------------------------------------------------ write-up
def f3(x):
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.3f}"


def mci(d, nd=3):
    return f"{d['value']:.{nd}f} [{d['ci95'][0]:.{nd}f}, {d['ci95'][1]:.{nd}f}]" if d["ci95"][0] is not None else f"{d['value']:.{nd}f}"


def render_md(R):
    ch = R["selected"]
    md = ["# R2b: final selection round (event-level criterion)", ""]
    md += ["Simulator only (uniform shear building, stationary band-limited excitation, modelled gx). Baselines assume quiet commissioning. Events on an uninstrumented floor are reported only.", "",
           f"- seeds: {R['seed_blocks']} (fresh; disjoint from every earlier block including R2's 5,100,000 / 5,200,000, asserted). R2's test block was NOT used here.",
           f"- healthy monitoring per building: {HL_NW} windows (level 0.5-1.5x) + {NZ_NW} noise-only windows = {(HL_NW + NZ_NW) * SF_SECONDS / 3600:.4f} h; bootstrap {R['setup']['bootstrap_resamples']} resamples by building.",
           "- Criterion: maximise mean(LOCAL event recall, BUILDING event recall) subject to false onsets <= 1.0 per hour on development healthy monitoring. LOCAL = mean of dense impacts / sparse impact / frequency shift, BUILDING = mean of 2x / 3x / 5x. Event detected = correct class output for >= 1 superframe in the event's recording (after debounce).", ""]
    md += ["## Development table (primary pair), all 63 candidates", "",
           "Columns: false onsets/h on healthy+noise monitoring; event recall (any) LOCAL and BUILDING and their mean; per kind; onset latency (superframes); events with a wrong-class event output; per-superframe LOCAL / BUILDING recall.", "",
           "| config | false onsets/h | LOCAL | BUILDING | mean | dense | sparse | shift | 2x | 3x | 5x | lat dense | lat sparse | wrong LOCAL | wrong BLD | sf LOCAL | sf BLD |", "|" + "---|" * 17]
    for r in sorted(R["development_table_primary_pair"], key=sel_key, reverse=True):
        mark = " **(chosen)**" if r["name"] == ch["name"] else ""
        el = "" if r["eligible"] else " (over budget)"
        md.append(f"| {r['name']}{mark}{el} | {r['false_onsets_per_hour']:.2f} | {f3(r['det_local_recall'])} | {f3(r['det_building_recall'])} | {f3(r['det_mean'])} | "
                  f"{f3(r['recall_det_dense'])} | {f3(r['recall_det_sparse'])} | {f3(r['recall_det_shift'])} | {f3(r['recall_det_x2'])} | {f3(r['recall_det_x3'])} | {f3(r['recall_det_x5'])} | "
                  f"{f3(r['latency_dense'])} | {f3(r['latency_sparse'])} | {f3(r['wrong_class_local'])} | {f3(r['wrong_class_building'])} | {f3(r['superframe_local_recall'])} | {f3(r['superframe_building_recall'])} |")
    md += ["", f"Chosen: **{ch['name']}** (budget 1.0/h {'met' if ch['constraint_met'] else 'NOT met: lowest false-onset rate taken'}).", "",
           "### Trade-off: best configuration under each false-onset budget (development)", "",
           "| budget /h | config | false onsets/h | LOCAL | BUILDING | mean | sparse | isolated dense windows (sf) |", "|---|---|---|---|---|---|---|---|"]
    for b, r in R["tradeoff"].items():
        md.append(f"| {b} | {r['name']}{'' if r['budget_met'] else ' (budget not met)'} | {r['false_onsets_per_hour']:.2f} | {f3(r['det_local_recall'])} | {f3(r['det_building_recall'])} | {f3(r['det_mean'])} | "
                  f"{f3(r['recall_det_sparse'])} | {f3(r['isolated_dense_window_recall'])} |")
    for pn, rep in R["test"].items():
        md += ["", f"## Test block, {pn} pair (floors {rep['floors']}) - chosen configuration, scored once", "",
               f"- monitoring {rep['monitoring_hours']:.1f} h, {rep['n_buildings']} buildings; events: {rep['n_events']}",
               f"- **false onsets per hour {mci(rep['false_onsets_per_hour'], 2)}** (healthy {mci(rep['false_onsets_per_hour_healthy'], 2)}, noise-only {mci(rep['false_onsets_per_hour_noise'], 2)}; LOCAL {mci(rep['false_local_onsets_per_hour'], 2)}, BUILDING {mci(rep['false_building_onsets_per_hour'], 2)})",
               f"- LOCAL event recall {mci(rep['det_local_recall'])}: dense {mci(rep['recall_det_dense'])}, sparse {mci(rep['recall_det_sparse'])}, shift {mci(rep['recall_det_shift'])}",
               f"- BUILDING event recall {mci(rep['det_building_recall'])}: 2x {mci(rep['recall_det_x2'])}, 3x {mci(rep['recall_det_x3'])}, 5x {mci(rep['recall_det_x5'])}",
               f"- mean {mci(rep['det_mean'])}; in-span mean {mci(rep['span_mean'])} (LOCAL {mci(rep['span_local_recall'])}, BUILDING {mci(rep['span_building_recall'])})",
               f"- onset latency (superframes, detected events): dense {mci(rep['latency_dense'], 2)}, sparse {mci(rep['latency_sparse'], 2)}, shift {mci(rep['latency_shift'], 2)}, 2x {mci(rep['latency_x2'], 2)}, 3x {mci(rep['latency_x3'], 2)}, 5x {mci(rep['latency_x5'], 2)}",
               f"- events with a wrong-class event output: dense {mci(rep['wrong_class_dense'])}, sparse {mci(rep['wrong_class_sparse'])}, shift {mci(rep['wrong_class_shift'])}, 2x {mci(rep['wrong_class_x2'])}, 3x {mci(rep['wrong_class_x3'])}, 5x {mci(rep['wrong_class_x5'])}",
               f"- per superframe (R2-style): ambient recall on monitoring {mci(rep['ambient_recall_superframe'])}; LOCAL correct-slave recall {mci(rep['superframe_local_recall'])} (wrong slave {mci(rep['superframe_wrong_slave'])}); BUILDING recall {mci(rep['superframe_building_recall'])}; isolated dense-impact windows {mci(rep['isolated_dense_window_recall'])} (n={rep['n_isolated_dense_windows']})",
               "- uninstrumented floor, share of recordings with any LOCAL/BUILDING output (reported only): " + "; ".join(
                   f"{k} {mci(v['any_event_output'])}" for k, v in rep["uninstrumented_reported_only"].items())]
    rf = R["reference_r2_config_on_r2b_test"]
    md += ["", "## Reference only: the R2 configuration (d, T=3, smoothed, k=1) on the R2b test block, primary pair", "",
           f"False onsets/h {mci(rf['false_onsets_per_hour'], 2)}; LOCAL {mci(rf['det_local_recall'])} (sparse {mci(rf['recall_det_sparse'])}); BUILDING {mci(rf['det_building_recall'])}. This is a comparison, not a selection.", ""]
    xc = R["reference_vs_vectorised"]
    md += [f"Reference check (scalar RefFusion with debounce vs vectorised rules): {xc['superframes_checked']} superframes, {xc['mismatches']} mismatches.", ""]
    return "\n".join(md) + "\n"


def merge(od, r2b):
    """fusion_eval.json / .md = both rounds."""
    src = od if (od / "fusion_eval_r2.json").exists() else OUT_DIR
    r2 = json.loads((src / "fusion_eval_r2.json").read_text())
    merged = {"rounds": {"R2": r2, "R2b": r2b},
              "note": "R2 (per-superframe AMBIENT recall >= 0.95 criterion) was scored once on its test block (seeds 5,200,000+); R2b (event-level criterion, FINAL) uses fresh blocks 6,100,000+ / 6,200,000+.",
              "final_selected": r2b["selected"]}
    fe.jdump(merged, od / "fusion_eval.json")
    intro = ["# Fusion evaluation: two rounds", "",
             "- **R2 (first criterion, reported, superseded).** Selection rule: maximise mean(LOCAL recall, BUILDING recall) subject to per-superframe AMBIENT recall >= 0.95. "
             "That allows about 126 false flags per hour and pushed the choice to the smoothed tree variant (rule d, T=3): BUILDING recall 0.43 and isolated-impact recall 0.43. "
             "Per-superframe scoring is the wrong unit for an alarm system. R2's test block (seeds 5,200,000+) was used for R2's single scoring only.",
             "- **R2b (final).** Event-level criterion (false onsets per hour, event recall), debounce k as an extra candidate dimension, fresh seed blocks 6,100,000+ (development) and 6,200,000+ (test). The R2 test block is not used.", "",
             "---", "", "# Round R2b (final)", ""]
    r2b_md = render_md(r2b).split("\n", 2)[2]
    r2_md = (src / "fusion_eval_r2.md").read_text().replace("\n#", "\n##", 1000).replace("# R2: master", "## R2: master", 1)
    (od / "fusion_eval.md").write_text("\n".join(intro) + r2b_md + "\n---\n\n# Round R2 (first criterion, superseded)\n\n" + r2_md, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--small", action="store_true")
    ap.add_argument("--prime", choices=["dev", "test"], default=None)
    ap.add_argument("--cache", type=Path, default=None, help="simulation cache directory (default: .cache/fusion_eval_r2b/cache, or $RESONANCE_CACHE_DIR/fusion_eval_r2b/cache)")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    out = a.out or (SCRATCH / "small_out_r2b" if a.small else None)
    run(small=a.small, cache_dir=a.cache, out_dir=out, prime=a.prime)


if __name__ == "__main__":
    main()
