"""R2: master-side fusion (AMBIENT / LOCAL(slave) / BUILDING) evaluated on simulated buildings, with a fixed selection rule.

Setup (fixed in this file before any test-block number was looked at):
  slaves     two slaves on floors (5, 10) = primary pair, and (3, 7) = secondary pair (reported only). Each slave runs the old
             tree (models/selected, model.h thresholds) + noise gate (az_std < 92.12 => normal) + 2-of-3 smoothing, and reports
             raw class, smoothed state and az_std every 1.4 s superframe (1 window = 1 superframe).
  baseline   per slave, median az_std (float32; mean of the two middle values for 20 windows) of a separate healthy
             commissioning recording (20 windows, level 1.0).
  rules      a, b, c, d x T in {1.5, 2, 3} x tree variant {smoothed: state==faulty ; raw: raw==faulty or state==faulty}
             (rule b does not use the tree: one variant). r_i = az_std_i / baseline_i, e_i = (r_i >= T) smoothed 2-of-3.
  data       simulated buildings, seed blocks above 5,000,000 and disjoint from every earlier block (asserted):
             development block (selection) and test block (scored once with the chosen configuration).
  scenarios  per building: healthy ambient (level 0.5-1.5x of commissioning), noise-only, impact and frequency shift on each of
             floors 3, 5, 7, 10 (event on the slave's floor), strong building-wide 2x/3x/5x (every window), impact and shift on
             an uninstrumented floor (reported only). 30 windows each.
  truth      AMBIENT: healthy / noise windows and non-event windows of event recordings; LOCAL(i): windows containing the event on
             slave i's floor (building_sim window truth); BUILDING: every window of 2x/3x/5x.
  selection  development block, primary pair only: maximise mean(LOCAL recall with the correct slave, BUILDING recall) subject to
             AMBIENT recall >= 0.95; ties: higher AMBIENT recall, then simpler rule (a < b < c < d), then lower T, then smoothed
             variant. If none meets the constraint, the highest AMBIENT recall is taken (and said so).
Also contains RefFusion, the scalar Python reference whose behaviour firmware/esp32/include/fusion.h must match exactly
(see firmware/esp32/tools/gen_fusion_vectors.py).

Usage:  python models/training/fusion_eval.py            full run (writes models/selected/validation/fusion_eval.{json,md})
        python models/training/fusion_eval.py --small    small subset into the scratch directory (determinism demo)
Resumable: simulation chunks are cached in --cache DIR (default $RESONANCE_CACHE_DIR/fusion_eval/cache, else <repo>/.cache/fusion_eval/cache; key = simulator/tree/source hash, seeds, spec).
"""
import argparse
import hashlib
import inspect
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "data" / "generator"))
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import building_sim as bs  # noqa: E402
import localisation_gate as lg  # noqa: E402
import train_building as tb  # noqa: E402  (seed blocks only)

import cachedir  # noqa: E402

SCRATCH = cachedir.script_dir("fusion_eval")   # cache, log and --small output; see cachedir.py (--cache / RESONANCE_CACHE_DIR / .cache/)
OUT_DIR = ROOT / "models" / "selected" / "validation"
SEED = 42
N = 128
NW = 30                 # windows (= superframes) per recording
COMM_NW = 20
SF_SECONDS = 1.4
GATE32 = np.float32(92.12)  # tiers.h NOISE_GATE_COUNTS
FLOORS = (3, 5, 7, 10)
PAIRS = {"primary": (5, 10), "secondary": (3, 7)}
UNINSTR_FLOORS = (1, 2, 4, 6, 8, 9)
T_VALUES = (1.5, 2.0, 3.0)
RULES = "abcd"
OTHER_MAX = 1.5         # rule c: r_other < OTHER_MAX
N_BOOT = 2000
AMB_MIN = 0.95
CHUNK = 20
BLOCKS = {"dev": (5_100_000, 160), "test": (5_200_000, 160), "small_dev": (5_900_000, 12), "small_test": (5_950_000, 12)}
CLS_NAMES = ["COMMISSIONING", "AMBIENT", "LOCAL", "BUILDING", "NO_DATA"]
OUT_NAMES = ["AMBIENT", "LOCAL(1)", "LOCAL(2)", "BUILDING"]  # evaluation output / truth codes 0..3
EXC = {"comm": 0, "healthy": 1, "noise": 2, "s3": 3, "s5": 4, "s2": 5, "uimp": 300, "ushift": 301}
STRONG = {"s2": 2.0, "s3": 3.0, "s5": 5.0}
LOG_PATH = SCRATCH / "fusion_eval.log"


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + s + "\n")
    except OSError:
        pass


def jdump(obj, path):
    path.write_text(json.dumps(obj, indent=1, sort_keys=False, default=lambda o: o.tolist() if isinstance(o, np.ndarray) else o.item() if isinstance(o, np.generic) else str(o)))


# ======================================================================================================================
# Scalar reference of the firmware fusion (must match firmware/esp32/include/fusion.h bit for bit)
# ======================================================================================================================
C_COMMISSIONING, C_AMBIENT, C_LOCAL, C_BUILDING, C_NO_DATA = range(5)
K_DECISION, K_BEGIN, K_CHANGE, K_STATUS = 0, 1, 2, 3  # K_DECISION is only used in the parity files


def smooth_update(hist, raw, state):
    """smooth.h: state becomes class c once it has >= 2 of the last 3 raw values, else holds."""
    hist[0], hist[1], hist[2] = hist[1], hist[2], raw
    for c in range(3):
        if (hist[0] == c) + (hist[1] == c) + (hist[2] == c) >= 2:
            return c
    return state


class RefFusion:
    """Event-driven reference: on_report / on_status / advance / flush, closing superframes exactly like Fusion in fusion.h.

    Records (tuples): decisions (sf, cls, slave, degraded, r1, r2) and records (kind, cls, slave, degraded, sf, t_ms, r1, r2,
    base1, base2, alive_mask, closed, n_events); r = -1.0 when unknown, base = 0.0 when not yet known.

    Rule f (Phase C2a, the deployed rule; fusion_eval_r3.py flags_for(): "b OR e"): per slave the std-ratio flag (r >= T_b) and the
    AE flag (ae_ready and not az_std < gate and ae >= T, T = the AE threshold) are each smoothed 2-of-3 with their OWN history,
    e_i = b_i or a_i, then decided as rule b (degraded: LOCAL(alive) iff e). Both histories are held while a slave has no report and
    cleared on ALIVE after DEAD and on a stream restart. on_report takes ae (float32) and ae_ready for it; the other rules ignore them."""
    BASE_MIN = np.float32(1.0)

    def __init__(self, rule, T, use_raw, comm_n=20, status_every=64, max_catchup=32, debounce=1, T_b=3.0, gate=GATE32):
        self.rule, self.T, self.use_raw = rule, np.float32(T), bool(use_raw)
        self.T_b, self.gate = np.float32(T_b), np.float32(gate)
        self.hist_a = [[-1, -1, -1], [-1, -1, -1]]
        self.astate = [0, 0]
        self.debounce = int(debounce)
        self.out_key, self.cand, self.cand_n = None, None, 0
        self.comm_n, self.status_every, self.max_catchup = comm_n, status_every, max_catchup
        self.other_max = np.float32(OTHER_MAX)
        self.base = [np.float32(0), np.float32(0)]
        self.base_ok = [False, False]
        self.comm_buf = [[], []]
        self.hist = [[-1, -1, -1], [-1, -1, -1]]
        self.estate = [0, 0]
        self.alive = [True, True]
        self.started = False
        self.next_sf = 0
        self.slot_sf = 0
        self.slots = [None, None]
        self.armed = False
        self.armed_sf = 0
        self.closed = 0
        self.n_events = 0
        self.key = None
        self.rejected = 0
        self.restarts = 0
        self.decisions, self.records = [], []

    # ---- inputs
    def on_report(self, slave, sf, raw, state, f0, now_ms, ae=0.0, ae_ready=False):
        f0 = np.float32(f0)
        if not np.isfinite(f0) or f0 < 0:
            self.rejected += 1
            return
        i = slave - 1
        if not self.started:
            self.started = True
            self.next_sf = sf
            self.slots = [None, None]
        elif sf < self.next_sf:
            self._restart(sf, now_ms)
        self._close_upto(sf, now_ms)
        if self.slots[0] is None and self.slots[1] is None:
            self.slot_sf = sf
        elif self.slot_sf != sf:
            self.slots = [None, None]
            self.slot_sf = sf
        self.slots[i] = (raw, state, f0, np.float32(ae), bool(ae_ready))
        if self.slots[0] is not None and self.slots[1] is not None:
            self._close_upto(sf + 1, now_ms)

    def on_status(self, slave, alive, now_ms):
        i = slave - 1
        if alive and not self.alive[i]:
            self.hist[i] = [-1, -1, -1]
            self.estate[i] = 0
            self.hist_a[i] = [-1, -1, -1]
            self.astate[i] = 0
        self.alive[i] = bool(alive)

    def advance(self, master_sf, polling_active, now_ms):
        if polling_active:
            if not self.started:
                self.started = True
                self.next_sf = master_sf
            elif master_sf < self.next_sf:
                self._restart(master_sf, now_ms)
            self.armed, self.armed_sf = True, master_sf
            self._close_upto(master_sf, now_ms)
        elif self.armed and self.started and master_sf == self.armed_sf:
            self._close_upto(master_sf + 1, now_ms)

    def flush(self, now_ms):
        if self.started and (self.slots[0] is not None or self.slots[1] is not None) and self.slot_sf >= self.next_sf:
            self._close_upto(self.slot_sf + 1, now_ms)

    # ---- internals
    def _restart(self, new_sf, now_ms):
        if (self.slots[0] is not None or self.slots[1] is not None) and self.slot_sf >= self.next_sf:
            self._close_upto(self.slot_sf + 1, now_ms)
        self.slots = [None, None]
        self.next_sf = new_sf
        self.hist = [[-1, -1, -1], [-1, -1, -1]]
        self.estate = [0, 0]
        self.hist_a = [[-1, -1, -1], [-1, -1, -1]]
        self.astate = [0, 0]
        self.key = None
        self.out_key, self.cand, self.cand_n = None, None, 0
        self.armed = False
        self.restarts += 1

    def _close_upto(self, target, now_ms):
        if target <= self.next_sf:
            return
        if target - self.next_sf > self.max_catchup:
            self.next_sf = target - self.max_catchup
        while self.next_sf < target:
            sl = self.slots if (self.slots[0] is not None or self.slots[1] is not None) and self.slot_sf == self.next_sf else [None, None]
            self._finalize(self.next_sf, sl, now_ms)
            if sl is self.slots:
                self.slots = [None, None]
            self.next_sf += 1
        if self.slots[0] is not None or self.slots[1] is not None:
            if self.slot_sf < self.next_sf:
                self.slots = [None, None]

    def _ratio(self, i, f0):
        b = self.base[i]
        if b < self.BASE_MIN:
            b = self.BASE_MIN
        return np.float32(f0) / b

    def _finalize(self, sf, sl, now_ms):
        self.closed += 1
        neg = np.float32(-1.0)
        if not (self.base_ok[0] and self.base_ok[1]):
            for i in range(2):
                if sl[i] is not None and not self.base_ok[i]:
                    self.comm_buf[i].append(sl[i][2])
                    if len(self.comm_buf[i]) == self.comm_n:
                        s = sorted(self.comm_buf[i])
                        n = self.comm_n
                        self.base[i] = s[n // 2] if n % 2 else np.float32((s[n // 2 - 1] + s[n // 2]) * np.float32(0.5))
                        self.base_ok[i] = True
            res = (C_COMMISSIONING, 0, 0, neg, neg)
        else:
            res = self._decide(sl)
        res = self._debounce(res)
        cls, slave, deg, r1, r2 = res
        self.decisions.append((sf, cls, slave, deg, r1, r2))
        key = (cls, slave, deg)
        if self.key is None or key != self.key:
            was_event = self.key is not None and self.key[0] in (C_LOCAL, C_BUILDING)
            kind = K_BEGIN if cls in (C_LOCAL, C_BUILDING) and not was_event else K_CHANGE
            self.n_events += 1
            self._record(kind, res, sf, now_ms)
            self.key = key
        if self.closed % self.status_every == 0:
            self._record(K_STATUS, res, sf, now_ms)

    def _debounce(self, res):
        """R2b: a LOCAL(i)/BUILDING key that differs from the current output must hold `debounce` consecutive superframes before it is
        output. Meanwhile AMBIENT is reported (or, if an event class is already output, that event class is kept). Any non-event
        decision is output at once and clears the count."""
        cls, slave, deg, r1, r2 = res
        key = (cls, slave, deg)
        if cls not in (C_LOCAL, C_BUILDING):
            self.cand, self.cand_n = None, 0
            self.out_key = key
            return res
        if self.out_key == key:
            self.cand, self.cand_n = None, 0
            return res
        if self.cand == key:
            self.cand_n += 1
        else:
            self.cand, self.cand_n = key, 1
        if self.cand_n >= self.debounce:
            self.cand, self.cand_n = None, 0
            self.out_key = key
            return res
        if self.out_key is not None and self.out_key[0] in (C_LOCAL, C_BUILDING):
            return (self.out_key[0], self.out_key[1], self.out_key[2], r1, r2)
        return (C_AMBIENT, 0, deg, r1, r2)

    def _record(self, kind, res, sf, now_ms):
        cls, slave, deg, r1, r2 = res
        am = (1 if self.alive[0] else 0) | (2 if self.alive[1] else 0)
        self.records.append((kind, cls, slave, deg, sf, now_ms, r1, r2, self.base[0] if self.base_ok[0] else np.float32(0),
                             self.base[1] if self.base_ok[1] else np.float32(0), am, self.closed, self.n_events))

    def _decide(self, sl):
        neg = np.float32(-1.0)
        r = [neg, neg]
        e = [False, False]
        F = [False, False]
        for i in range(2):
            if sl[i] is None:
                continue
            raw, state, f0, ae, ae_ready = sl[i]
            r[i] = self._ratio(i, f0)
            tb = self.T_b if self.rule == "f" else self.T
            self.estate[i] = smooth_update(self.hist[i], 1 if r[i] >= tb else 0, self.estate[i])
            e[i] = self.estate[i] == 1
            if self.rule == "f":
                x = ae_ready and not (f0 < self.gate) and ae >= self.T
                self.astate[i] = smooth_update(self.hist_a[i], 1 if x else 0, self.astate[i])
                e[i] = e[i] or self.astate[i] == 1
            F[i] = (state == 2) or (self.use_raw and raw == 2)
        n = (sl[0] is not None) + (sl[1] is not None)
        if n == 0:
            return (C_NO_DATA, 0, 0, neg, neg)
        if n == 1:
            i = 0 if sl[0] is not None else 1
            if self.rule in ("b", "f"):
                loc = e[i]
            elif self.rule == "c":
                loc = F[i] and e[i]
            else:  # a, d
                loc = F[i]
            return (C_LOCAL if loc else C_AMBIENT, i + 1 if loc else 0, i + 1, r[0], r[1])
        if self.rule == "a":
            if F[0] and F[1]:
                return (C_BUILDING, 0, 0, r[0], r[1])
            if F[0] or F[1]:
                return (C_LOCAL, 1 if F[0] else 2, 0, r[0], r[1])
        elif self.rule in ("b", "f"):
            if e[0] and e[1]:
                return (C_BUILDING, 0, 0, r[0], r[1])
            if e[0] or e[1]:
                return (C_LOCAL, 1 if e[0] else 2, 0, r[0], r[1])
        elif self.rule == "c":
            if e[0] and e[1]:
                return (C_BUILDING, 0, 0, r[0], r[1])
            if F[0] and e[0] and r[1] < self.other_max:
                return (C_LOCAL, 1, 0, r[0], r[1])
            if F[1] and e[1] and r[0] < self.other_max:
                return (C_LOCAL, 2, 0, r[0], r[1])
        else:  # d
            if e[0] and e[1]:
                return (C_BUILDING, 0, 0, r[0], r[1])
            if F[0] and not F[1]:
                return (C_LOCAL, 1, 0, r[0], r[1])
            if F[1] and not F[0]:
                return (C_LOCAL, 2, 0, r[0], r[1])
            if F[0] and F[1]:
                return (C_LOCAL, 1 if r[0] >= r[1] else 2, 0, r[0], r[1])
        return (C_AMBIENT, 0, 0, r[0], r[1])


# ======================================================================================================================
# seeds
# ======================================================================================================================
def prior_max_seed():
    return max(tb.prior_max_seed(), max(a + n for a, n in tb.BLOCKS.values()))


def assert_seed_blocks_disjoint():
    prior = prior_max_seed()
    rng = sorted((a, a + n, k) for k, (a, n) in BLOCKS.items())
    assert rng[0][0] > 5_000_000 and rng[0][0] >= prior, (rng[0], prior)
    assert all(rng[i][1] <= rng[i + 1][0] for i in range(len(rng) - 1)), "fusion_eval seed blocks overlap"
    return prior


def seeds_of(name):
    a, n = BLOCKS[name]
    return list(range(a, a + n))


# ======================================================================================================================
# simulation (cached per chunk of buildings)
# ======================================================================================================================
def level_mult(seed, exc):
    return float(np.random.default_rng([0xA, int(seed), int(exc)]).uniform(0.5, 1.5))


def uninstr_floor(seed, kind):
    return UNINSTR_FLOORS[(int(seed) + (3 if kind == "shift" else 0)) % len(UNINSTR_FLOORS)]


def rec_names():
    names = ["healthy", "noise", "s2", "s3", "s5", "uimp", "ushift"]
    names += [f"imp{f}" for f in FLOORS] + [f"shift{f}" for f in FLOORS]
    return names


def chunk_key(seeds):
    h = hashlib.sha256()
    h.update(Path(bs.__file__).read_bytes())
    h.update((ROOT / "firmware/esp32/include/model.h").read_bytes())
    h.update(inspect.getsource(lg.window_features).encode())
    h.update(inspect.getsource(simulate_chunk).encode())
    h.update(json.dumps({"seeds": [int(s) for s in seeds], "nw": NW, "comm_nw": COMM_NW, "exc": EXC, "floors": FLOORS, "unf": UNINSTR_FLOORS,
                         "strong": STRONG, "version": 1}, sort_keys=True).encode())
    return h.hexdigest()


def simulate_chunk(seeds):
    tree = lg.Tree()
    ps = [bs.building_params(s) for s in seeds]
    nb = len(ps)
    out = {"seeds": np.array(seeds)}

    def record(exc, nwin, amp=1.0, zero=False, mult=None, kind=None, floors=None):
        sig = np.zeros((nb, bs.NFLOOR, nwin * N)) if zero else bs.simulate_batch(ps, exc, nwin, None, "default", amp)
        if mult is not None:
            sig = sig * mult[:, None, None]
        std = np.empty((nb, bs.NFLOOR, nwin), dtype=np.float32)
        cls = np.empty((nb, bs.NFLOOR, nwin), dtype=np.uint8)
        mask = np.zeros((nb, nwin), dtype=bool)
        for i, p in enumerate(ps):
            kw = {}
            if kind == "imp":
                kw["impact_floor"] = int(floors[i])
            elif kind == "shift":
                kw["shift_floor"] = int(floors[i])
            az, gx, tr = bs.sensor_output(p, sig[i], exc, return_truth=True, **kw)
            a_std, a_kurt, a_dom = lg.window_features(bs.to_windows(az))
            g_std, _, g_dom = lg.window_features(bs.to_windows(gx))
            std[i] = a_std.astype(np.float32)
            cls[i] = tree.predict_model_h({"az_std": a_std, "az_kurt": a_kurt, "az_domfreq": a_dom, "gx_std": g_std, "gx_domfreq": g_dom})
            m = tr["impact_mask"] if kind == "imp" else tr["shift_mask"] if kind == "shift" else None
            if m is not None:
                mask[i] = m
        return std, cls, mask

    out["comm_std"], _, _ = record(EXC["comm"], COMM_NW)
    mh = np.array([level_mult(p["seed"], EXC["healthy"]) for p in ps])
    out["healthy_std"], out["healthy_cls"], _ = record(EXC["healthy"], NW, mult=mh)
    out["noise_std"], out["noise_cls"], _ = record(EXC["noise"], NW, zero=True)
    for nm, a in STRONG.items():
        out[nm + "_std"], out[nm + "_cls"], _ = record(EXC[nm], NW, amp=a)
    for kind in ("imp", "shift"):
        for f in FLOORS:
            exc = (100 if kind == "imp" else 200) + f
            m = np.array([level_mult(p["seed"], exc) for p in ps])
            out[f"{kind}{f}_std"], out[f"{kind}{f}_cls"], out[f"{kind}{f}_mask"] = record(exc, NW, mult=m, kind=kind, floors=np.full(nb, f))
    for kind, nm in (("imp", "uimp"), ("shift", "ushift")):
        m = np.array([level_mult(p["seed"], EXC[nm]) for p in ps])
        fl = np.array([uninstr_floor(p["seed"], kind) for p in ps])
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
        f = cache_dir / f"{tag}_{sd[0]}_{len(sd)}_{key[:16]}.npz"
        z = None
        if f.exists():
            try:
                z = dict(np.load(f))
                assert str(z["key"]) == key and np.array_equal(z["seeds"], sd)
            except Exception:  # noqa: BLE001 - corrupt or stale cache: recompute
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


def smooth_seq(a):
    """2-of-3 smoothing along the last axis of an int array (any leading shape)."""
    sh = a.shape
    return lg.smooth_sequences(a.reshape(-1, sh[-1]).astype(int)).reshape(sh)


def derive(D):
    """Per-recording gated raw class and smoothed state (all floors), baselines, float32 az_std."""
    R = {}
    comm = np.sort(D["comm_std"], axis=2)
    n = comm.shape[2]
    base = ((comm[..., n // 2 - 1] + comm[..., n // 2]) * np.float32(0.5)).astype(np.float32)  # (nb, 10)
    for nm in rec_names():
        std = D[nm + "_std"]
        raw = np.where(std < GATE32, 0, D[nm + "_cls"]).astype(int)
        R[nm] = {"std": std, "raw": raw, "state": smooth_seq(raw), "mask": D.get(nm + "_mask")}
        if nm in ("uimp", "ushift"):
            R[nm]["floor"] = D[nm + "_floor"]
    return R, base


# ======================================================================================================================
# vectorised rules
# ======================================================================================================================
def configs():
    cfgs = []
    for rule in RULES:
        for T in T_VALUES:
            for use_raw in ((False,) if rule == "b" else (False, True)):
                cfgs.append({"rule": rule, "T": T, "use_raw": use_raw, "name": f"{rule}, T={T:g}, {'raw' if use_raw else 'smoothed'}" if rule != "b" else f"b, T={T:g}, no tree"})
    return cfgs


class PairView:
    """Floors (fa, fb) of a block: float32 ratios, tree outputs, e-flags (cached per T)."""

    def __init__(self, R, base, fa, fb):
        self.fa, self.fb = fa, fb
        self.base = np.maximum(base[:, [fa - 1, fb - 1]], np.float32(1.0))  # (nb, 2)
        self.rec = {}
        for nm, r in R.items():
            sel = [fa - 1, fb - 1]
            if nm in ("uimp", "ushift"):
                continue
            std = r["std"][:, sel]
            self.rec[nm] = {"std": std, "r": (std / self.base[:, :, None]).astype(np.float32), "raw": r["raw"][:, sel], "state": r["state"][:, sel], "mask": r["mask"]}
        self._e = {}

    def e(self, nm, T):
        k = (nm, T)
        if k not in self._e:
            self._e[k] = smooth_seq((self.rec[nm]["r"] >= np.float32(T)).astype(int)) == 1
        return self._e[k]


def decide_vec(cfg, v, nm):
    """-> out codes (nb, nw): 0 AMBIENT, 1 LOCAL(1), 2 LOCAL(2), 3 BUILDING (both slaves reporting)."""
    rec = v.rec[nm]
    e = v.e(nm, cfg["T"])
    r = rec["r"]
    F = (rec["state"] == 2) | ((rec["raw"] == 2) if cfg["use_raw"] else False)
    e1, e2, F1, F2, r1, r2 = e[:, 0], e[:, 1], F[:, 0], F[:, 1], r[:, 0], r[:, 1]
    rule = cfg["rule"]
    if rule == "a":
        return np.where(F1 & F2, 3, np.where(F1, 1, np.where(F2, 2, 0)))
    if rule == "b":
        return np.where(e1 & e2, 3, np.where(e1, 1, np.where(e2, 2, 0)))
    if rule == "c":
        l1 = F1 & e1 & (r2 < np.float32(OTHER_MAX))
        l2 = F2 & e2 & (r1 < np.float32(OTHER_MAX))
        return np.where(e1 & e2, 3, np.where(l1, 1, np.where(l2, 2, 0)))
    both = np.where(r1 >= r2, 1, 2)
    return np.where(e1 & e2, 3, np.where(F1 & ~F2, 1, np.where(F2 & ~F1, 2, np.where(F1 & F2, both, 0))))


def decide_degraded_vec(cfg, v, nm, i):
    """Slave i (0/1) alone: LOCAL(i) (code i+1) or AMBIENT (0)."""
    rec = v.rec[nm]
    e = v.e(nm, cfg["T"])[:, i]
    F = (rec["state"][:, i] == 2) | ((rec["raw"][:, i] == 2) if cfg["use_raw"] else False)
    loc = e if cfg["rule"] == "b" else (F & e) if cfg["rule"] == "c" else F
    return np.where(loc, i + 1, 0)


def scenario_truth(v, name):
    """Truth codes (nb, nw) per scenario name for the pair view."""
    nb = next(iter(v.rec.values()))["std"].shape[0]
    z = np.zeros((nb, NW), dtype=int)
    if name in ("healthy", "noise"):
        return z
    if name in ("s2", "s3", "s5"):
        return z + 3
    kind, sl = name.split("_")
    f = v.fa if sl == "s1" else v.fb
    m = v.rec[f"{kind}{f}"]["mask"]
    return np.where(m, 1 if sl == "s1" else 2, 0)


def scenario_rec(v, name):
    if name in ("healthy", "noise", "s2", "s3", "s5"):
        return name
    kind, sl = name.split("_")
    return f"{kind}{v.fa if sl == 's1' else v.fb}"


SCEN = ["healthy", "noise", "imp_s1", "imp_s2", "shift_s1", "shift_s2", "s2", "s3", "s5"]


def tally(cfg, v):
    """Per-building counts: counts[scen] (nb, 4 truth, 4 out); iso (nb, 2) hits/total over isolated impact windows,
    clu (nb, 2) over impact windows that have a neighbouring impact window."""
    counts = {}
    nb = next(iter(v.rec.values()))["std"].shape[0]
    iso = np.zeros((nb, 2))
    clu = np.zeros((nb, 2))
    for s in SCEN:
        out = decide_vec(cfg, v, scenario_rec(v, s))
        tr = scenario_truth(v, s)
        c = np.zeros((nb, 4, 4))
        for t in range(4):
            for o in range(4):
                c[:, t, o] = ((tr == t) & (out == o)).sum(axis=1)
        counts[s] = c
        if s.startswith("imp"):
            m = tr > 0
            nbr = np.zeros_like(m)
            nbr[:, 1:] |= m[:, :-1]
            nbr[:, :-1] |= m[:, 1:]
            hit = out == tr
            iso[:, 0] += (hit & m & ~nbr).sum(axis=1)
            iso[:, 1] += (m & ~nbr).sum(axis=1)
            clu[:, 0] += (hit & m & nbr).sum(axis=1)
            clu[:, 1] += (m & nbr).sum(axis=1)
    return counts, iso, clu


def agg(counts, names):
    return sum(counts[n] for n in names)


GROUPS = {"all": SCEN, "impact": ["imp_s1", "imp_s2"], "shift": ["shift_s1", "shift_s2"], "local": ["imp_s1", "imp_s2", "shift_s1", "shift_s2"],
          "building": ["s2", "s3", "s5"]}


def metrics_of(C):
    """C (..., 4, 4) pooled truth x out counts -> dict of arrays."""
    def div(a, b):
        return np.divide(a, b, out=np.full(np.shape(a), np.nan), where=np.asarray(b) > 0)
    amb_n = C[..., 0, :].sum(-1)
    loc_n = C[..., 1, :].sum(-1) + C[..., 2, :].sum(-1)
    bld_n = C[..., 3, :].sum(-1)
    correct = C[..., 1, 1] + C[..., 2, 2]
    anyloc = C[..., 1, 1] + C[..., 1, 2] + C[..., 2, 1] + C[..., 2, 2]
    anyev = anyloc + C[..., 1, 3] + C[..., 2, 3]
    amb_rec = div(C[..., 0, 0], amb_n)
    return {"ambient_recall": amb_rec, "ambient_false_event_rate": 1 - amb_rec, "false_events_per_hour": (1 - amb_rec) * 3600.0 / SF_SECONDS,
            "local_recall": div(correct, loc_n), "local_any_event": div(anyev, loc_n), "local_wrong_slave": div(C[..., 1, 2] + C[..., 2, 1], loc_n),
            "local_slave_correct_given_local_out": div(correct, anyloc), "local_called_building": div(C[..., 1, 3] + C[..., 2, 3], loc_n),
            "building_recall": div(C[..., 3, 3], bld_n), "building_called_local": div(C[..., 3, 1] + C[..., 3, 2], bld_n),
            "building_called_ambient": div(C[..., 3, 0], bld_n)}


def select_key(row):
    """Sort key, larger is better: mean(LOCAL, BUILDING), ambient recall, simpler rule, lower T, smoothed variant."""
    return (row["mean_local_building"], row["ambient_recall"], -RULES.index(row["rule"]), -row["T"], -int(row["use_raw"]))


def dev_table(cfgs, v):
    rows = []
    for cfg in cfgs:
        counts, iso, clu = tally(cfg, v)
        m = metrics_of(agg(counts, SCEN).sum(0))
        ml = metrics_of(agg(counts, GROUPS["local"]).sum(0))
        mb = metrics_of(agg(counts, GROUPS["building"]).sum(0))
        row = {"name": cfg["name"], "rule": cfg["rule"], "T": cfg["T"], "use_raw": cfg["use_raw"], "ambient_recall": float(m["ambient_recall"]),
               "false_events_per_hour": float(m["false_events_per_hour"]), "local_recall": float(ml["local_recall"]),
               "building_recall": float(mb["building_recall"]), "isolated_impact_recall": float(iso[:, 0].sum() / max(iso[:, 1].sum(), 1)),
               "n_isolated_impact_windows": int(iso[:, 1].sum())}
        row["mean_local_building"] = (row["local_recall"] + row["building_recall"]) / 2
        row["eligible"] = bool(row["ambient_recall"] >= AMB_MIN)
        rows.append(row)
    return rows


def choose(rows):
    elig = [r for r in rows if r["eligible"]]
    if elig:
        return max(elig, key=select_key), True
    return max(rows, key=lambda r: (r["ambient_recall"],) + select_key(r)), False


# ======================================================================================================================
# bootstrap by building
# ======================================================================================================================
def boot_weights(nb, B, seed=SEED):
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, nb, (B, nb))
    return np.stack([np.bincount(r, minlength=nb) for r in idx]).astype(float)


def ci(x):
    x = np.asarray(x)
    x = x[~np.isnan(x)]
    return [float(np.percentile(x, 2.5)), float(np.percentile(x, 97.5))] if len(x) else [None, None]


def with_ci(Cb, W, keys=None):
    """Cb (nb, 4, 4) per-building pooled counts -> {metric: {value, ci}}."""
    pt = metrics_of(Cb.sum(0))
    bs_ = metrics_of(np.einsum("bn,nij->bij", W, Cb))
    return {k: {"value": float(pt[k]), "ci95": ci(bs_[k])} for k in (keys or pt)}


def ratio_ci(num, den, W):
    v = num.sum() / max(den.sum(), 1)
    bsv = (W @ num) / np.maximum(W @ den, 1)
    return {"value": float(v), "ci95": ci(bsv), "n": int(den.sum())}


def full_report(cfg, v, W):
    counts, iso, clu = tally(cfg, v)
    tot = agg(counts, SCEN)
    R = {"config": cfg["name"], "n_buildings": int(tot.shape[0]), "n_superframes_scored": int(tot.sum())}
    R["pooled"] = with_ci(tot, W)
    # confusion matrix (counts and row-normalised) pooled over all scenarios; AMBIENT truth includes non-event windows
    Cs = tot.sum(0)
    bsC = np.einsum("bn,nij->bij", W, tot)
    rows = bsC.sum(2, keepdims=True)
    frac_b = bsC / np.maximum(rows, 1)
    R["confusion"] = {"labels": OUT_NAMES, "counts": Cs.astype(int).tolist(), "row_fraction": (Cs / np.maximum(Cs.sum(1, keepdims=True), 1)).tolist(),
                      "row_fraction_ci95": [[ci(frac_b[:, i, j]) for j in range(4)] for i in range(4)]}
    R["groups"] = {g: with_ci(agg(counts, GROUPS[g]), W, ["local_recall", "local_any_event", "local_wrong_slave", "building_recall", "ambient_recall", "false_events_per_hour"])
                   for g in ("impact", "shift")}
    R["building_by_level"] = {s: with_ci(counts[s], W, ["building_recall", "building_called_local", "building_called_ambient"]) for s in ("s2", "s3", "s5")}
    R["ambient_by_scenario"] = {s: with_ci(counts[s], W, ["ambient_recall", "false_events_per_hour"]) for s in ("healthy", "noise")}
    amb_ev = agg(counts, ["imp_s1", "imp_s2", "shift_s1", "shift_s2"])
    R["ambient_by_scenario"]["non_event_windows_of_event_recordings"] = with_ci(amb_ev, W, ["ambient_recall", "false_events_per_hour"])
    R["local_by_slave"] = {f"slave{i + 1}": ratio_ci(agg(counts, ["imp_s%d" % (i + 1), "shift_s%d" % (i + 1)])[:, i + 1, i + 1],
                                                     agg(counts, ["imp_s%d" % (i + 1), "shift_s%d" % (i + 1)])[:, i + 1, :].sum(1), W) for i in range(2)}
    R["isolated_impact"] = {"isolated": ratio_ci(iso[:, 0], iso[:, 1], W), "clustered": ratio_ci(clu[:, 0], clu[:, 1], W)}
    return R


# ======================================================================================================================
# extra analyses
# ======================================================================================================================
def isolated_both_variants(cfg, v, W):
    out = {}
    for ur in ((False, True) if cfg["rule"] != "b" else (False,)):
        c = dict(cfg, use_raw=ur)
        _, iso, clu = tally(c, v)
        out["raw_variant" if ur else "smoothed_variant"] = {"isolated": ratio_ci(iso[:, 0], iso[:, 1], W), "clustered": ratio_ci(clu[:, 0], clu[:, 1], W)}
    return out


def uninstrumented(cfg, v, R, base, W):
    """Reported only: outputs while the event is on an uninstrumented floor."""
    fa, fb = v.fa, v.fb
    out = {}
    nb = base.shape[0]
    sel = [fa - 1, fb - 1]
    for nm in ("uimp", "ushift"):
        r = R[nm]
        std = r["std"][:, sel]
        ratio = (std / v.base[:, :, None]).astype(np.float32)
        e = smooth_seq((ratio >= np.float32(cfg["T"])).astype(int)) == 1
        F = (r["state"][:, sel] == 2) | ((r["raw"][:, sel] == 2) if cfg["use_raw"] else False)
        pseudo = {"std": std, "r": ratio, "raw": r["raw"][:, sel], "state": r["state"][:, sel], "mask": r["mask"]}
        vv = PairView.__new__(PairView)
        vv.rec = {nm: pseudo}
        vv._e = {(nm, cfg["T"]): e}
        res = decide_vec(cfg, vv, nm)
        m = r["mask"]
        cnt = np.zeros((nb, 2, 4))
        for o in range(4):
            cnt[:, 0, o] = ((res == o) & m).sum(1)
            cnt[:, 1, o] = ((res == o) & ~m).sum(1)
        out[nm] = {"event_windows": {OUT_NAMES[o]: ratio_ci(cnt[:, 0, o], cnt[:, 0].sum(1), W) for o in range(4)},
                   "non_event_windows": {OUT_NAMES[o]: ratio_ci(cnt[:, 1, o], cnt[:, 1].sum(1), W) for o in range(4)},
                   "event_floor_by_building": "from seed, floors " + str(UNINSTR_FLOORS)}
    return out


def degraded_eval(cfg, v, W):
    """Only slave i alive: LOCAL(i) or AMBIENT. Truth: LOCAL(i) = event windows on slave i's floor; AMBIENT = windows with no event
    anywhere; windows with an event on the OTHER floor are not scored (not observable), BUILDING windows reported separately."""
    out = {}
    nb = next(iter(v.rec.values()))["std"].shape[0]
    for i in (0, 1):
        sl = "s%d" % (i + 1)
        amb = np.zeros((nb, 2))
        loc = np.zeros((nb, 2))
        bld = np.zeros((nb, 2))
        for s in SCEN:
            tr = scenario_truth(v, s)
            res = decide_degraded_vec(cfg, v, scenario_rec(v, s), i)
            if s in ("healthy", "noise"):
                amb += np.stack([(res == 0).sum(1), (tr == 0).sum(1)], 1)
            elif s in ("s2", "s3", "s5"):
                bld += np.stack([(res == i + 1).sum(1), (tr == 3).sum(1)], 1)
            else:
                mine = s.endswith(sl)
                if mine:
                    loc += np.stack([((res == i + 1) & (tr == i + 1)).sum(1), (tr == i + 1).sum(1)], 1)
                    amb += np.stack([((res == 0) & (tr == 0)).sum(1), (tr == 0).sum(1)], 1)
        out[f"only_slave{i + 1}_alive"] = {"ambient_recall": ratio_ci(amb[:, 0], amb[:, 1], W), "local_recall": ratio_ci(loc[:, 0], loc[:, 1], W),
                                           "building_windows_called_local": ratio_ci(bld[:, 0], bld[:, 1], W)}
    return out


def crosscheck_reference(cfgs, v, base, nbuild=3):
    """Run RefFusion on report sequences built from the data and compare with the vectorised rules (all configs)."""
    mism = checked = 0
    for cfg in cfgs:
        for nm in ("healthy", "imp%d" % v.fa, "shift%d" % v.fb, "s3", "noise", "s5"):
            rec = v.rec[nm]
            out = decide_vec(cfg, v, nm)
            for b in range(nbuild):
                rf = RefFusion(cfg["rule"], cfg["T"], cfg["use_raw"])
                sf = 0
                for w in range(COMM_NW):  # commissioning stretch from the base value itself
                    for i in (0, 1):
                        rf.on_report(i + 1, sf, 0, 0, v.base[b, i], sf)
                    sf += 1
                n_dec0 = len(rf.decisions)
                for w in range(NW):
                    for i in (0, 1):
                        rf.on_report(i + 1, sf, int(rec["raw"][b, i, w]), int(rec["state"][b, i, w]), rec["std"][b, i, w], sf)
                    sf += 1
                rf.flush(sf)
                dec = rf.decisions[n_dec0:]
                assert len(dec) == NW
                for w, d in enumerate(dec):
                    ref = {C_AMBIENT: 0, C_BUILDING: 3}.get(d[1], d[2] if d[1] == C_LOCAL else -1)
                    checked += 1
                    mism += int(ref != out[b, w])
    return {"superframes_checked": checked, "mismatches": mism}


# ======================================================================================================================
def run(small=False, cache_dir=None, out_dir=None, write=True):
    t0 = time.time()
    cache_dir = cache_dir or SCRATCH / "cache"
    prior = assert_seed_blocks_disjoint()
    dn, tn = ("small_dev", "small_test") if small else ("dev", "test")
    B = 200 if small else N_BOOT
    log(f"== fusion_eval small={small}: seed blocks {BLOCKS[dn]}, {BLOCKS[tn]} (earlier max seed {prior}, disjointness asserted)")
    cfgs = configs()
    blocks = {}
    for nm in (dn, tn):
        D = load_block(seeds_of(nm), cache_dir, nm)
        R, base = derive(D)
        blocks[nm] = (R, base)
    # ---- development selection (primary pair)
    Rd, based = blocks[dn]
    vd = PairView(Rd, based, *PAIRS["primary"])
    rows = dev_table(cfgs, vd)
    chosen_row, met = choose(rows)
    chosen = next(c for c in cfgs if c["name"] == chosen_row["name"])
    log(f"== development table done; chosen {chosen['name']} (constraint met: {met}) mean={chosen_row['mean_local_building']:.4f}")
    # ---- test, once
    Rt, baset = blocks[tn]
    res = {"chosen": dict(chosen_row, constraint_met=met), "pairs": {}}
    for pn, (fa, fb) in PAIRS.items():
        vt = PairView(Rt, baset, fa, fb)
        nb = vt.base.shape[0]
        W = boot_weights(nb, B)
        rep = full_report(chosen, vt, W)
        rep["floors"] = [fa, fb]
        rep["isolated_impact_both_variants"] = isolated_both_variants(chosen, vt, W)
        rep["uninstrumented_reported_only"] = uninstrumented(chosen, vt, Rt, baset, W)
        rep["degraded_mode"] = degraded_eval(chosen, vt, W)
        res["pairs"][pn] = rep
    # development block, chosen config, primary pair, for the dev-vs-test comparison
    Wd = boot_weights(vd.base.shape[0], B)
    res["development_primary_with_ci"] = full_report(chosen, vd, Wd)
    xc = crosscheck_reference(cfgs, vd, based)
    log(f"== reference vs vectorised: {xc}")
    out = {
        "task": "R2 master-side fusion evaluation", "small_mode": small,
        "seed_blocks": {k: {"start": a, "n": n} for k, (a, n) in BLOCKS.items() if k in (dn, tn)}, "earlier_max_seed": prior, "seed_disjointness_asserted": True,
        "setup": {"windows_per_recording": NW, "commissioning_windows": COMM_NW, "superframe_s": SF_SECONDS, "noise_gate_counts": float(GATE32), "pairs": PAIRS,
                  "T_values": T_VALUES, "rule_c_other_max": OTHER_MAX, "ambient_recall_min": AMB_MIN, "bootstrap_resamples": B, "bootstrap_unit": "building",
                  "scenarios": SCEN, "selection_metric": "mean(LOCAL recall with correct slave pooled over impact+shift, BUILDING recall pooled over 2x/3x/5x); ties: AMBIENT recall, simpler rule, lower T, smoothed variant"},
        "development_table_primary_pair": rows, "selected": chosen_row, "constraint_met": met, "reference_vs_vectorised": xc, "test": res,
    }
    if write:
        od = out_dir or OUT_DIR
        od.mkdir(parents=True, exist_ok=True)
        jdump(out, od / "fusion_eval_r2.json")
        (od / "fusion_eval_r2.md").write_text(render_md(out), encoding="utf-8")
        log(f"== wrote {od / 'fusion_eval_r2.json'}")
    log(f"== done in {time.time() - t0:.0f}s")
    return out


# ======================================================================================================================
def f3(x):
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.3f}"


def mci(d):
    return f"{f3(d['value'])} [{f3(d['ci95'][0])}, {f3(d['ci95'][1])}]"


def render_md(R):
    ch = R["selected"]
    md = ["# R2: master fusion evaluation (AMBIENT / LOCAL / BUILDING)", ""]
    md += ["Simulator only (building_sim: uniform shear building, stationary band-limited excitation, gx modelled). Baselines assume quiet commissioning. "
           "An event on an uninstrumented floor is a reported-only case. Selection on the development block, primary pair (floors 5, 10), test block scored once.", ""]
    md += [f"- seeds: {R['seed_blocks']} (all above 5,000,000, disjoint from earlier blocks, asserted)",
           f"- bootstrap: {R['setup']['bootstrap_resamples']} resamples by building, 95% percentile intervals", ""]
    md += ["## Development table (primary pair, development block)", "",
           "Rule: maximise mean(LOCAL recall, BUILDING recall) subject to AMBIENT recall >= 0.95. 'raw' = F is raw==faulty OR state==faulty; 'smoothed' = state==faulty.", "",
           "| config | AMBIENT recall | false events / h | LOCAL recall | BUILDING recall | mean | isolated-impact recall (n) | eligible |", "|---|---|---|---|---|---|---|---|"]
    for r in sorted(R["development_table_primary_pair"], key=select_key, reverse=True):
        mark = " **(chosen)**" if r["name"] == ch["name"] else ""
        md.append(f"| {r['name']}{mark} | {f3(r['ambient_recall'])} | {r['false_events_per_hour']:.1f} | {f3(r['local_recall'])} | {f3(r['building_recall'])} | {f3(r['mean_local_building'])} | "
                  f"{f3(r['isolated_impact_recall'])} ({r['n_isolated_impact_windows']}) | {r['eligible']} |")
    md += ["", f"Chosen: **{ch['name']}** (constraint AMBIENT >= {AMB_MIN} {'met' if R['constraint_met'] else 'NOT met: highest AMBIENT recall taken'}).", ""]
    for pn, rep in R["test"]["pairs"].items():
        p = rep["pooled"]
        md += [f"## Test block, {pn} pair (floors {rep['floors']}) - chosen configuration", "",
               f"- buildings {rep['n_buildings']}, superframes scored {rep['n_superframes_scored']}",
               f"- AMBIENT recall {mci(p['ambient_recall'])}; false events per hour {mci(p['false_events_per_hour'])}",
               f"- LOCAL recall (correct slave) {mci(p['local_recall'])}; any event {mci(p['local_any_event'])}; wrong slave {mci(p['local_wrong_slave'])}; "
               f"slave correct given LOCAL output {mci(p['local_slave_correct_given_local_out'])}; called BUILDING {mci(p['local_called_building'])}",
               f"- BUILDING recall {mci(p['building_recall'])}; called LOCAL {mci(p['building_called_local'])}; called AMBIENT {mci(p['building_called_ambient'])}", "",
               "Confusion matrix (counts; rows = truth, columns = output):", "", "| truth \\ output | " + " | ".join(OUT_NAMES) + " |", "|---|---|---|---|---|"]
        for i, lab in enumerate(OUT_NAMES):
            md.append(f"| {lab} | " + " | ".join(str(c) for c in rep["confusion"]["counts"][i]) + " |")
        md += ["", "Row fractions with 95% intervals:", "", "| truth \\ output | " + " | ".join(OUT_NAMES) + " |", "|---|---|---|---|---|"]
        for i, lab in enumerate(OUT_NAMES):
            md.append(f"| {lab} | " + " | ".join(f"{f3(rep['confusion']['row_fraction'][i][j])} [{f3(rep['confusion']['row_fraction_ci95'][i][j][0])}, {f3(rep['confusion']['row_fraction_ci95'][i][j][1])}]" for j in range(4)) + " |")
        md += ["", "Splits:", "", "| subset | LOCAL recall (correct slave) | any event | BUILDING recall | called LOCAL | AMBIENT recall |", "|---|---|---|---|---|---|"]
        for g in ("impact", "shift"):
            x = rep["groups"][g]
            md.append(f"| {g} | {mci(x['local_recall'])} | {mci(x['local_any_event'])} | - | - | {mci(x['ambient_recall'])} |")
        for s, x in rep["building_by_level"].items():
            md.append(f"| {s[1]}x building-wide | - | - | {mci(x['building_recall'])} | {mci(x['building_called_local'])} | - |")
        md += ["", "AMBIENT recall by source:", ""]
        for s, x in rep["ambient_by_scenario"].items():
            md.append(f"- {s}: {mci(x['ambient_recall'])}, {mci(x['false_events_per_hour'])} false events/h")
        md += ["", "LOCAL recall per slave: " + "; ".join(f"{k} {mci(x)}" for k, x in rep["local_by_slave"].items()), "",
               "Isolated-impact recall (impact windows with no neighbouring impact window; LOCAL(correct slave) required):", ""]
        for k, x in rep["isolated_impact_both_variants"].items():
            md.append(f"- {k}: isolated {mci(x['isolated'])} (n={x['isolated']['n']}); clustered {mci(x['clustered'])} (n={x['clustered']['n']})")
        md += ["", "Degraded mode (one slave alive; only LOCAL(alive) or AMBIENT possible):", ""]
        for k, x in rep["degraded_mode"].items():
            md.append(f"- {k}: AMBIENT recall {mci(x['ambient_recall'])}; LOCAL recall {mci(x['local_recall'])}; BUILDING windows called LOCAL(alive) {mci(x['building_windows_called_local'])}")
        md += ["", "Uninstrumented-floor events (reported only; no truth defined; share of output):", ""]
        for k, x in rep["uninstrumented_reported_only"].items():
            md.append(f"- {k} event windows: " + ", ".join(f"{o} {mci(x['event_windows'][o])}" for o in OUT_NAMES))
            md.append(f"- {k} non-event windows: " + ", ".join(f"{o} {mci(x['non_event_windows'][o])}" for o in OUT_NAMES))
        md.append("")
    xc = R["reference_vs_vectorised"]
    md += ["## Reference check", "", f"Scalar RefFusion vs the vectorised rules: {xc['superframes_checked']} superframes, {xc['mismatches']} mismatches.", ""]
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--small", action="store_true", help="12+12 buildings, 200 resamples, output to the scratch directory")
    ap.add_argument("--cache", type=Path, default=None, help="simulation cache directory (default: .cache/fusion_eval/cache, or $RESONANCE_CACHE_DIR/fusion_eval/cache)")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    out = a.out or (SCRATCH / "small_out" if a.small else None)
    run(small=a.small, cache_dir=a.cache, out_dir=out)


if __name__ == "__main__":
    main()
