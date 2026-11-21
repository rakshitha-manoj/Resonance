"""Phase A: first-natural-frequency tracking -> DAMAGE channel (simulation only).

Why: the event system detects transient vibration EVENTS, not structural DAMAGE (30% storey stiffness loss is detected only 17% of
the time). Classic SHM watches the first natural frequency f1: damage lowers it. Each slave averages its last M zoom spectra
(firmware/esp32/include/f1track.h, spec_zoom 3..12 Hz) and reports the relative drop of f1 against its OWN commissioned f1_0
(median of the first C valid estimates; the estimator has a per-building bias of about +-0.1 Hz, so never compare to the model f1);
the master pools the slaves' drops and raises DAMAGE after P consecutive superframes >= delta (firmware/esp32/include/damage.h).
Everything below runs the C code through the ctypes bridge (shm.py / tools/shm_lib.c).

PRE-REGISTERED (written before any development result was looked at)
  data      per building, slaves on floors 5/10 (primary pair) and 3/7 (secondary), az at 100 Hz from building_sim.
            Healthy record: a 192-window commissioning segment (level 1.0) + 19 segments of 128 windows (level 0.5-1.5x per
            segment) = 2624 windows = 1.02 h at one window per 1.4 s superframe. Environment: uniform stiffness drift
            eps(t) = A sin(2 pi t / 1800 s + phi_b) applied to all storeys (p["f1"] * sqrt(1 + eps), constant inside a segment,
            value at the segment mid-time), A in {0, 0.5%, 1%}; the PRIMARY condition is A = 0.5%.
            Damage recordings: the same 192 commissioning windows, then 200 damaged windows (4.7 min; level 0.5-1.5x, eps at
            t = 1900 s) for storey s in {1,3,5,8,10} x loss d in {0.05,0.10,0.15,0.20,0.30} (25 cells) at A = 0.5%; for A = 0
            and A = 1% only 9 cells (s in {1,5,10} x d in {0.10,0.20,0.30}, compute budget). The detector runs over the 192
            + 200 windows; detection = a DAMAGE begin inside the 200 damaged windows (DAMAGE already active at their start,
            i.e. a false begin before, is not a detection).
  seeds     dev 6,500,000 + 120 buildings, test 6,600,000 + 120 (small: 6,950,500 + 12 / 6,951,000 + 12), asserted disjoint from
            every earlier block (fusion_eval BLOCKS, fusion_eval_r2b BLOCKS2) and from the ranges reserved for other phases
            (6,300,000-6,300,999, 6,400,000-6,400,999).
  selection DEVELOPMENT block, primary condition (A = 0.5%), primary pair (floors 5/10), grid M {16, 32} x C {32, 64, 128} x
            delta {0.4, 0.6, 0.8, 1.0, 1.25, 1.5, 2, 2.5, 3, 4 %} x P {2, 4, 8, 16, 32} superframes (300 configs).
            Objective: maximise the mean detection rate over the 25 damage cells (equal weights) subject to false DAMAGE
            begins <= 1 per 24 h of healthy monitoring (pooled over the dev buildings, from the superframe at which the
            commissioning is complete). Ties: lower false rate, smaller M, smaller C, smaller P, larger delta. If no
            config meets the budget: the lowest false rate. The chosen config is then scored ONCE on the test block
            (95% bootstrap intervals by building, 2000 resamples) for all three A and both pairs; nothing is tuned on test.
  reported  (i) f1 estimation error vs the eigen f1 on healthy windows, (ii) the physics curve (eigen f1 drop vs d per storey,
            independent of any estimator), (iii) estimated vs true drop, (iv) detection rate and latency per cell, false
            DAMAGE per 24 h at each A, both pairs, (v) a coarser-zoom-grid study (0.1 Hz step) on dev buildings.
Usage: freq_tracking_eval.py [--small] [--prime dev|test] [--workers N] [--batch B] [--cache DIR] [--out DIR]
Cache: --cache DIR, else $RESONANCE_CACHE_DIR/freq_tracking_eval/cache, else <repo>/.cache/freq_tracking_eval/cache (per-chunk
per-window f1 estimates only, no spectra). Run with the .venv python (numpy/scipy; no matplotlib: see freq_tracking_figs.py).
"""
import argparse
import hashlib
import inspect
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import warnings

import numpy as np
warnings.simplefilter("ignore", RuntimeWarning)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import cachedir  # noqa: E402

ROOT = cachedir.ROOT
sys.path.insert(0, str(ROOT / "data" / "generator"))
import building_sim as bs  # noqa: E402
import frontend  # noqa: E402
import shm  # noqa: E402

SCRATCH = cachedir.script_dir("freq_tracking_eval")
OUT_DIR = ROOT / "models" / "selected" / "validation"
LOG_PATH = SCRATCH / "freq_tracking_eval.log"

N = 128
SF = 1.4                                  # superframe = one window per slave (s)
FLOORS = (3, 5, 7, 10)
PAIRS = {"primary": (5, 10), "secondary": (3, 7)}
MS = (16, 32)
CS = (32, 64, 128)
A_ENV = (0.0, 0.005, 0.01)
IA_PRIMARY = 1
T_ENV = 1800.0
COMM_NW, SEG_NW, N_SEG = 192, 128, 19
HEALTHY_NW = COMM_NW + N_SEG * SEG_NW     # 2624 windows = 1.02 h
DMG_NW = 200
T_DMG = 1800.0 + DMG_NW * SF / 2
STOREYS = (1, 3, 5, 8, 10)
LOSSES = (0.05, 0.10, 0.15, 0.20, 0.30)
CELLS = [(s, d) for s in STOREYS for d in LOSSES]            # 25, index = si * 5 + di
CELLS_SECONDARY = [i for i, (s, d) in enumerate(CELLS) if s in (1, 5, 10) and d in (0.10, 0.20, 0.30)]   # 9
DELTAS = (0.004, 0.006, 0.008, 0.010, 0.0125, 0.015, 0.020, 0.025, 0.030, 0.040)
PS = (2, 4, 8, 16, 32)
FA_BUDGET_24H = 1.0
N_BOOT = 2000
SEED = 42
BLOCKS = {"dev": (6_500_000, 120), "test": (6_600_000, 120), "small_dev": (6_950_500, 12), "small_test": (6_951_000, 12)}
RESERVED = {"phase_B": (6_300_000, 1000), "phase_D": (6_400_000, 1000)}
EXC_H, EXC_D = 10, 100
CHUNK, CHUNK_SMALL = 12, 3
EX_CELL = CELLS.index((3, 0.20))          # example plotted in the figures (fixed before scoring)


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + s + chr(10))
    except OSError:
        pass


# ------------------------------------------------------------------ seeds
def assert_seed_blocks_disjoint():
    import fusion_eval as fe
    import fusion_eval_r2b as r2b
    prior = max(fe.prior_max_seed(), max(a + n for a, n in fe.BLOCKS.values()))
    mine = sorted((a, a + n, k) for k, (a, n) in BLOCKS.items())
    others = [(a, a + n, "fusion_eval:" + k) for k, (a, n) in fe.BLOCKS.items()]
    others += [(a, a + n, "r2b:" + k) for k, (a, n) in r2b.BLOCKS2.items()]
    others += [(a, a + n, "reserved:" + k) for k, (a, n) in RESERVED.items()]
    assert mine[0][0] >= prior, (mine[0], prior)
    allr = sorted(mine + others)
    for i in range(len(allr) - 1):
        assert allr[i][1] <= allr[i + 1][0], f"seed blocks overlap: {allr[i]} / {allr[i + 1]}"
    return prior


def seeds_of(name):
    a, n = BLOCKS[name]
    return list(range(a, a + n))


# ------------------------------------------------------------------ environment and simulation
def level_mult(seed, exc):
    return float(np.random.default_rng([0xA, int(seed), int(exc)]).uniform(0.5, 1.5))


def eps_of(seed, a_env, t):
    phi = np.random.default_rng([0xE5, int(seed)]).uniform(0, 2 * np.pi)
    return a_env * np.sin(2 * np.pi * t / T_ENV + phi)


def scaled(p, eps):
    return dict(p, f1=p["f1"] * np.sqrt(1.0 + eps))


def cells_for(ia):
    return list(range(len(CELLS))) if ia == IA_PRIMARY else CELLS_SECONDARY


def az_floors(ps, pj, exc, nwin, damages, mult, batch):
    """(nb, 4, nwin*128) int16 az of the four slave floors; building seeds / noise from ps, stiffness from pj."""
    sig = bs.simulate_many(pj, exc, nwin, damages, "default", 1.0, batch=batch)
    out = np.empty((len(ps), len(FLOORS), nwin * N), np.int16)
    for i, p in enumerate(ps):
        s = sig[i] * (mult[i] if mult is not None else 1.0)
        az, _gx = bs.sensor_output(p, s, exc)
        out[i] = az[[f - 1 for f in FLOORS]]
    return out


def track_floor(z, zc):
    """Tracker runs for one slave: est/valid per M (C-independent), f1_0 and commissioning window per (M, C)."""
    n = z.shape[0]
    est = np.empty((len(MS), n), np.float32)
    val = np.zeros((len(MS), n), np.uint8)
    f10 = np.empty((len(MS), len(CS)), np.float32)
    wc = np.empty((len(MS), len(CS)), np.int32)
    for im, m in enumerate(MS):
        for ic, c in enumerate(CS):
            r = shm.track(z, m, c)
            f10[im, ic] = r["f1_0"]
            wc[im, ic] = r["wcomm"]
            if ic == 0:
                est[im] = r["est"]
                val[im] = r["valid"]
    return est, val, f10, wc


def simulate_chunk(seeds, batch):
    """All simulation + tracker runs for a chunk of buildings, all three A. Returns only per-window f1 estimates."""
    ps = [bs.building_params(s) for s in seeds]
    nb, nA, nF, nM, nC = len(ps), len(A_ENV), len(FLOORS), len(MS), len(CS)
    out = {"seeds": np.array(seeds), "f1_base": np.array([p["f1"] for p in ps], np.float32)}
    out["h_est"] = np.full((nA, nb, nF, nM, HEALTHY_NW), np.nan, np.float32)
    out["h_valid"] = np.zeros((nA, nb, nF, nM, HEALTHY_NW), np.uint8)
    out["h_f10"] = np.full((nA, nb, nF, nM, nC), np.nan, np.float32)
    out["h_wc"] = np.full((nA, nb, nF, nM, nC), -1, np.int32)
    out["h_f1seg"] = np.zeros((nA, nb, N_SEG + 1), np.float32)
    out["d_est"] = np.full((nA, nb, nF, nM, len(CELLS), DMG_NW), np.nan, np.float32)
    out["d_valid"] = np.zeros((nA, nb, nF, nM, len(CELLS), DMG_NW), np.uint8)
    out["d_f1true"] = np.full((nA, nb, len(CELLS)), np.nan, np.float32)
    for ia, a_env in enumerate(A_ENV):
        # ---- healthy record
        az = np.empty((nb, nF, HEALTHY_NW * N), np.int16)
        w0 = 0
        for j in range(N_SEG + 1):
            nw = COMM_NW if j == 0 else SEG_NW
            tmid = (w0 + nw / 2) * SF
            eps = [eps_of(p["seed"], a_env, tmid) for p in ps]
            pj = [scaled(p, e) for p, e in zip(ps, eps)]
            out["h_f1seg"][ia, :, j] = [bs.f1_hz(q) for q in pj]
            mult = None if j == 0 else np.array([level_mult(p["seed"], EXC_H + j) for p in ps])
            az[:, :, w0 * N:(w0 + nw) * N] = az_floors(ps, pj, EXC_H + j, nw, None, mult, batch)
            w0 += nw
        zh = np.empty((nb, nF, HEALTHY_NW, frontend.ZOOM_N), np.float32)
        for i in range(nb):
            for f in range(nF):
                zh[i, f] = frontend.zoom(az[i, f].reshape(HEALTHY_NW, N))
                (out["h_est"][ia, i, f], out["h_valid"][ia, i, f], out["h_f10"][ia, i, f],
                 out["h_wc"][ia, i, f]) = track_floor(zh[i, f], None)
        del az
        # ---- damage recordings (commissioning windows = the first 192 healthy windows)
        pj = [scaled(p, eps_of(p["seed"], a_env, T_DMG)) for p in ps]
        for k in cells_for(ia):
            s, d = CELLS[k]
            exc = EXC_D + k
            mult = np.array([level_mult(p["seed"], exc) for p in ps])
            out["d_f1true"][ia, :, k] = [bs.f1_hz(q, (s, d)) for q in pj]
            azd = az_floors(ps, pj, exc, DMG_NW, [(s, d)] * nb, mult, batch)
            for i in range(nb):
                for f in range(nF):
                    zd = frontend.zoom(azd[i, f].reshape(DMG_NW, N))
                    zz = np.concatenate([zh[i, f, :COMM_NW], zd])
                    for im, m in enumerate(MS):
                        r = shm.track(zz, m, CS[0])
                        out["d_est"][ia, i, f, im, k] = r["est"][COMM_NW:]
                        out["d_valid"][ia, i, f, im, k] = r["valid"][COMM_NW:]
    return out


def _chunk_job(seeds, batch):
    t = time.time()
    return simulate_chunk(seeds, batch), time.time() - t


def chunk_key(seeds):
    h = hashlib.sha256()
    h.update(Path(bs.__file__).read_bytes())
    for f in ("f1track.h", "spectrum.h", "spectrum_tables.h"):
        h.update((frontend.FW / "include" / f).read_bytes())
    h.update(shm.SRC.read_bytes())
    for fn in (simulate_chunk, track_floor, az_floors, eps_of, level_mult, scaled, cells_for):
        h.update(inspect.getsource(fn).encode())
    h.update(json.dumps({"seeds": [int(s) for s in seeds], "ms": MS, "cs": CS, "a": A_ENV, "env": T_ENV, "comm": COMM_NW, "seg": SEG_NW,
                         "nseg": N_SEG, "dmg": DMG_NW, "tdmg": T_DMG, "cells": CELLS, "sec": CELLS_SECONDARY, "floors": FLOORS,
                         "version": 1}, sort_keys=True).encode())
    return h.hexdigest()


def load_block(seeds, cache_dir, tag, workers, batch, chunk):
    cache_dir.mkdir(parents=True, exist_ok=True)
    chunks = [seeds[i:i + chunk] for i in range(0, len(seeds), chunk)]
    paths, todo = {}, []
    for ci, sd in enumerate(chunks):
        key = chunk_key(sd)
        f = cache_dir / f"ft_{tag}_{sd[0]}_{len(sd)}_{key[:16]}.npz"
        paths[ci] = (f, key)
        ok = False
        if f.exists():
            try:
                z = np.load(f)
                ok = str(z["key"]) == key and np.array_equal(z["seeds"], sd)
            except Exception:  # noqa: BLE001
                ok = False
        if ok:
            log(f"  [{tag}] chunk {ci + 1}/{len(chunks)} loaded from cache")
        else:
            todo.append(ci)

    def store(ci, res, dt):
        f, key = paths[ci]
        tmp = f.with_suffix(".tmp.npz")
        np.savez(tmp, key=np.array(key), **res)
        tmp.replace(f)
        log(f"  [{tag}] simulated chunk {ci + 1}/{len(chunks)} ({len(chunks[ci])} buildings) in {dt:.0f}s")

    if todo and workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_chunk_job, chunks[ci], batch): ci for ci in todo}
            for fu in as_completed(futs):
                res, dt = fu.result()
                store(futs[fu], res, dt)
    else:
        for ci in todo:
            res, dt = _chunk_job(chunks[ci], batch)
            store(ci, res, dt)
    parts = []
    for ci in range(len(chunks)):
        z = dict(np.load(paths[ci][0]))
        z.pop("key", None)
        parts.append(z)
    ax = lambda k: 0 if k in ("seeds", "f1_base") else 1  # noqa: E731
    return {k: np.concatenate([p[k] for p in parts], axis=ax(k)) for k in parts[0]}


# ------------------------------------------------------------------ drops and detection
def pair_idx(pair):
    return [FLOORS.index(f) for f in pair]


def healthy_drop(D, ia, im, ic, pair):
    """(nb, n, 2) float32 drop series and (nb, n, 2) usable flags, exactly the tracker's f1t_drop (float32 arithmetic)."""
    idx = pair_idx(pair)
    est = D["h_est"][ia][:, idx, im]            # (nb, 2, n)
    val = D["h_valid"][ia][:, idx, im].astype(bool)
    f10 = D["h_f10"][ia][:, idx, im, ic]        # (nb, 2)
    wc = D["h_wc"][ia][:, idx, im, ic]
    w = np.arange(est.shape[2])[None, None, :]
    ok = val & (wc[:, :, None] >= 0) & (w >= wc[:, :, None]) & np.isfinite(f10)[:, :, None]
    with np.errstate(all="ignore"):
        drop = (f10[:, :, None] - est) / f10[:, :, None]
    drop = np.where(ok, drop, np.float32(np.nan)).astype(np.float32)
    return drop.transpose(0, 2, 1), ok.transpose(0, 2, 1), f10, wc


def damage_drop(D, ia, im, ic, pair, cells, hd, hok, f10):
    """(nb, ncell, COMM_NW + DMG_NW, 2): the commissioning windows' drops (shared with the healthy record) + damaged windows."""
    idx = pair_idx(pair)
    est = D["d_est"][ia][:, idx, im][:, :, cells]   # (nb, 2, ncell, DMG_NW)
    val = D["d_valid"][ia][:, idx, im][:, :, cells].astype(bool)
    with np.errstate(all="ignore"):
        dd = (f10[:, :, None, None] - est) / f10[:, :, None, None]
    ok = val & np.isfinite(f10)[:, :, None, None]
    dd = np.where(ok, dd, np.float32(np.nan)).astype(np.float32).transpose(0, 2, 3, 1)   # (nb, ncell, DMG_NW, 2)
    ok = ok.transpose(0, 2, 3, 1)
    nb, nc = dd.shape[:2]
    head = np.broadcast_to(hd[:, None, :COMM_NW], (nb, nc, COMM_NW, 2))
    headok = np.broadcast_to(hok[:, None, :COMM_NW], (nb, nc, COMM_NW, 2))
    return np.concatenate([head, dd], axis=2), np.concatenate([headok, ok], axis=2)


class Cond:
    """Drop series of one (A, M, C, pair): healthy (nb, n, 2) and damage (nb, ncell, 392, 2)."""

    def __init__(self, D, ia, im, ic, pair):
        self.ia, self.im, self.ic, self.pair = ia, im, ic, pair
        self.cells = cells_for(ia)
        self.hd, self.hok, f10, wc = healthy_drop(D, ia, im, ic, pair)
        self.nb = self.hd.shape[0]
        self.n_eval = HEALTHY_NW - np.where((wc >= 0).any(1), np.where(wc >= 0, wc, HEALTHY_NW).min(1), HEALTHY_NW)   # windows from first commissioned
        self.dd, self.dok = damage_drop(D, ia, im, ic, pair, self.cells, self.hd, self.hok, f10)

    def run(self, delta, p):
        nbg, _, fb, _ = shm.detect(self.hd, self.hok, delta, p, 0)
        nb, ncell = self.dd.shape[:2]
        n = self.dd.shape[2]
        nbd, fbd, act, _ = shm.detect(self.dd.reshape(nb * ncell, n, 2), self.dok.reshape(nb * ncell, n, 2), delta, p, COMM_NW)
        fbd = fbd.reshape(nb, ncell)
        det = fbd >= 0
        return {"begins": nbg.astype(float), "hours": self.n_eval * SF / 3600.0, "det": det.astype(float),
                "lat": np.where(det, fbd - COMM_NW, np.nan).astype(float), "active_at_start": act.reshape(nb, ncell)}


def agg(x, W):
    return x.sum(0) if W is None else np.tensordot(W, x, axes=(1, 0))


def metrics(r, W=None):
    """False DAMAGE per 24 h, per-cell detection and latency (superframes), overall means; W (B, nb) bootstrap weights."""
    m = {"fa_per_24h": agg(r["begins"], W) / agg(r["hours"], W) * 24.0, "hours": float(r["hours"].sum())}
    nb = r["det"].shape[0]
    den = float(nb) if W is None else W.sum(1)[:, None]
    m["det_cell"] = agg(r["det"], W) / den
    dl = np.nan_to_num(r["lat"], nan=0.0) * r["det"]
    with np.errstate(all="ignore"):
        m["lat_cell"] = agg(dl, W) / agg(r["det"], W)
    m["det_mean"] = m["det_cell"].mean(-1)
    return m


def cell_label(k):
    s, d = CELLS[k]
    return f"s{s}_d{int(round(d * 100)):02d}"


# ------------------------------------------------------------------ selection (development)
def select(D):
    rows = []
    t0 = time.time()
    for im, m in enumerate(MS):
        for ic, c in enumerate(CS):
            cond = Cond(D, IA_PRIMARY, im, ic, PAIRS["primary"])
            for delta in DELTAS:
                for p in PS:
                    r = cond.run(delta, p)
                    mt = metrics(r)
                    rows.append({"M": m, "C": c, "delta": delta, "P": p, "fa_per_24h": float(mt["fa_per_24h"]), "false_begins": int(r["begins"].sum()),
                                 "monitoring_hours": float(mt["hours"]), "det_mean": float(mt["det_mean"]),
                                 "det_by_loss": {str(d): float(np.mean([mt["det_cell"][i] for i, (s, dd) in enumerate(CELLS) if dd == d])) for d in LOSSES},
                                 "lat_mean": float(np.nanmean(mt["lat_cell"])) if np.isfinite(mt["lat_cell"]).any() else None})
    for r in rows:
        r["eligible"] = bool(r["fa_per_24h"] <= FA_BUDGET_24H)
    log(f"== dev grid: {len(rows)} configs evaluated in {time.time() - t0:.0f}s")
    key = lambda r: (r["det_mean"], -r["fa_per_24h"], -r["M"], -r["C"], -r["P"], r["delta"])  # noqa: E731
    elig = [r for r in rows if r["eligible"]]
    if elig:
        ch = max(elig, key=key)
    else:
        ch = min(rows, key=lambda r: (r["fa_per_24h"], -r["det_mean"]))
    return rows, dict(ch, constraint_met=bool(elig)), key


# ------------------------------------------------------------------ reports
def ci(x):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    return [float(np.percentile(x, 2.5)), float(np.percentile(x, 97.5))] if len(x) else [None, None]


def boot_weights(nb, B, seed=SEED):
    idx = np.random.default_rng(seed).integers(0, nb, (B, nb))
    return np.stack([np.bincount(r, minlength=nb) for r in idx]).astype(float)


def vci(point, boot):
    return {"value": None if point is None or not np.isfinite(point) else float(point), "ci95": ci(boot)}


def score(D, cfg, B, with_example=False):
    """Selected config on a block, every A and both pairs."""
    im, ic = MS.index(cfg["M"]), CS.index(cfg["C"])
    nb = D["h_est"].shape[1]
    W = boot_weights(nb, B)
    rep = {}
    ex = None
    for ia, a_env in enumerate(A_ENV):
        for pn, pair in PAIRS.items():
            cond = Cond(D, ia, im, ic, pair)
            r = cond.run(cfg["delta"], cfg["P"])
            pt, bt = metrics(r), metrics(r, W)
            cells = cond.cells
            rr = {"false_begins": int(r["begins"].sum()), "monitoring_hours": float(pt["hours"]),
                  "fa_per_24h": vci(pt["fa_per_24h"], bt["fa_per_24h"]), "det_mean": vci(pt["det_mean"], bt["det_mean"]),
                  "cells": {}, "by_loss": {}, "by_storey": {}}
            for j, k in enumerate(cells):
                rr["cells"][cell_label(k)] = {"storey": CELLS[k][0], "loss": CELLS[k][1], "det": vci(pt["det_cell"][j], bt["det_cell"][:, j]),
                                              "latency_sf": vci(pt["lat_cell"][j], bt["lat_cell"][:, j]),
                                              "n_active_at_start": int(r["active_at_start"][:, j].sum())}
            for d in LOSSES:
                js = [j for j, k in enumerate(cells) if CELLS[k][1] == d]
                if js:
                    rr["by_loss"][str(d)] = vci(pt["det_cell"][js].mean(), bt["det_cell"][:, js].mean(1))
            for s in STOREYS:
                js = [j for j, k in enumerate(cells) if CELLS[k][0] == s]
                if js:
                    rr["by_storey"][str(s)] = vci(pt["det_cell"][js].mean(), bt["det_cell"][:, js].mean(1))
            common = [j for j, k in enumerate(cells) if k in CELLS_SECONDARY]
            rr["det_mean_common9"] = vci(pt["det_cell"][common].mean(), bt["det_cell"][:, common].mean(1))
            lat_all = r["lat"][np.isfinite(r["lat"])]
            rr["latency_sf_mean_detected"] = float(lat_all.mean()) if lat_all.size else None
            rep[f"A{a_env * 100:g}_{pn}"] = rr
            if with_example and ia == IA_PRIMARY and pn == "primary":
                ex = example(D, cond, cfg, cells.index(EX_CELL))
    return rep, ex


def example(D, cond, cfg, j):
    """Series for the figure: building 0, cell (storey 3, 20%), primary pair, A = 0.5%: per-slave f1 estimates, pooled drop, state."""
    b = 0
    dd, dok = cond.dd[b, j], cond.dok[b, j]
    nbg, fb, act, st = shm.detect(dd[None], dok[None], cfg["delta"], cfg["P"], COMM_NW, want_state=True)
    idx = pair_idx(cond.pair)
    im = MS.index(cfg["M"])
    est = np.concatenate([D["h_est"][cond.ia][b][idx, im][:, :COMM_NW], D["d_est"][cond.ia][b][idx, im][:, cond.cells[j]]], axis=1)
    with np.errstate(all="ignore"):
        pooled = np.nanmean(np.where(dok, dd, np.nan), axis=1)
    hp = np.nanmean(np.where(cond.hok[b], cond.hd[b], np.nan), axis=1)
    hst = shm.detect(cond.hd[b][None], cond.hok[b][None], cfg["delta"], cfg["P"], 0, want_state=True)[3][0]
    return {"seed": int(D["seeds"][b]), "storey": CELLS[cond.cells[j]][0], "loss": CELLS[cond.cells[j]][1], "damage_start_window": COMM_NW,
            "superframe_s": SF, "delta": cfg["delta"], "P": cfg["P"], "M": cfg["M"], "C": cfg["C"],
            "f1_true_baseline": float(D["h_f1seg"][cond.ia][b][0]), "f1_true_damaged": float(D["d_f1true"][cond.ia][b][cond.cells[j]]),
            "f1_est_slave_floors": [FLOORS[i] for i in idx], "f1_est": np.round(est, 4).tolist(),
            "pooled_drop": [None if not np.isfinite(v) else round(float(v), 6) for v in pooled], "damage_state": st[0].tolist(),
            "first_begin_window": int(fb[0]), "healthy_pooled_drop": [None if not np.isfinite(v) else round(float(v), 6) for v in hp],
            "healthy_damage_state": hst.tolist()}


def estimation_error(D):
    """(i) f1 error vs the eigen f1 on healthy windows, per A and M."""
    out = {}
    seg = np.concatenate([[0] * COMM_NW] + [[j] * SEG_NW for j in range(1, N_SEG + 1)]).astype(int)
    for ia, a_env in enumerate(A_ENV):
        truth = D["h_f1seg"][ia][:, seg]                    # (nb, n)
        for im, m in enumerate(MS):
            est = D["h_est"][ia][:, :, im]                  # (nb, 4, n)
            val = D["h_valid"][ia][:, :, im].astype(bool)
            err = est - truth[:, None, :]
            rec = {"valid_fraction_after_warmup": float(val[:, :, m - 1:].mean())}
            for tag, sel in (("all_floors", list(range(len(FLOORS)))), ("primary_pair", pair_idx(PAIRS["primary"])), ("secondary_pair", pair_idx(PAIRS["secondary"]))):
                e = err[:, sel][:, :, m - 1:]
                v = val[:, sel][:, :, m - 1:]
                ee = np.where(v, e, np.nan)
                a = np.abs(ee[v])
                bb = np.nanmean(ee, axis=(1, 2))
                rec[tag] = {"bias_hz": float(np.nanmean(ee)), "per_building_bias_sd_hz": float(np.nanstd(bb)), "mean_abs_hz": float(a.mean()),
                            "abs_q50": float(np.percentile(a, 50)), "abs_q90": float(np.percentile(a, 90)), "abs_q95": float(np.percentile(a, 95)),
                            "abs_q99": float(np.percentile(a, 99)), "abs_max": float(a.max()),
                            "within_building_sd_hz": float(np.nanmean(np.nanstd(ee, axis=2)))}
            out[f"A{a_env * 100:g}_M{m}"] = rec
    return out


def physics_curve(seeds):
    """(ii) eigen f1 drop vs loss per storey. For a uniform shear building the relative drop does not depend on f1; checked."""
    ps = [bs.building_params(s) for s in seeds[:20]]
    ds = [round(0.05 * i, 2) for i in range(1, 11)]
    res = {}
    for s in range(1, bs.NFLOOR + 1):
        row = []
        for d in ds:
            vals = np.array([1 - bs.f1_hz(p, (s, d)) / bs.f1_hz(p) for p in ps])
            assert vals.max() - vals.min() < 1e-9, "relative f1 drop should be building independent"
            row.append(float(vals[0]))
        res[str(s)] = row
    return {"loss": ds, "drop_by_storey": res, "grid_cells_drop_pct": {cell_label(k): 100 * float(1 - bs.f1_hz(ps[0], CELLS[k]) / bs.f1_hz(ps[0])) for k in range(len(CELLS))}}


def est_vs_true(D, cfg):
    """(iii) estimated vs true drop for the selected tracker, primary pair pooled, mean over the last 80 damaged windows."""
    im, ic = MS.index(cfg["M"]), CS.index(cfg["C"])
    out = {}
    for ia in (IA_PRIMARY, 0, 2):
        cond = Cond(D, ia, im, ic, PAIRS["primary"])
        est = np.nanmean(np.where(cond.dok[:, :, -80:], cond.dd[:, :, -80:], np.nan), axis=(2, 3))     # (nb, ncell)
        f10 = D["h_f1seg"][ia][:, 0]
        true = (f10[:, None] - D["d_f1true"][ia][:, cond.cells]) / f10[:, None]
        rows = {}
        for j, k in enumerate(cond.cells):
            e = est[:, j]
            rows[cell_label(k)] = {"storey": CELLS[k][0], "loss": CELLS[k][1], "true_drop_mean": float(true[:, j].mean()), "est_drop_mean": float(np.nanmean(e)),
                                   "est_drop_sd": float(np.nanstd(e)), "est_minus_true_rms": float(np.sqrt(np.nanmean((e - true[:, j]) ** 2)))}
        out[f"A{A_ENV[ia] * 100:g}"] = rows
    return out


def coarse_grid_study(seeds, batch):
    """(v) accuracy of the f1 estimate with the zoom evaluated on every k-th grid frequency, healthy, M = 16, floors 5/10."""
    ps = [bs.building_params(s) for s in seeds]
    nw = 192
    sig = bs.simulate_many(ps, 7, nw, None, "default", 1.0, batch=batch)
    res = {}
    zs = []
    for i, p in enumerate(ps):
        az, _ = bs.sensor_output(p, sig[i], 7)
        for f in PAIRS["primary"]:
            zs.append((p["f1"], frontend.zoom(bs.to_windows(az)[f - 1])))
    for k in (1, 2, 3, 4, 6, 10):
        errs, sds = [], []
        for f1, z in zs:
            zm = z.reshape(-1, 16, frontend.ZOOM_N).mean(axis=1)[:, ::k]          # 12 averages of 16 windows
            est = shm.peak_grid(zm, frontend.ZOOM_F0, frontend.ZOOM_DF * k)
            ok = (est > frontend.ZOOM_F0 + 1e-3) & (est < 12.0 - 1e-3)
            errs.append(np.where(ok, est - f1, np.nan))
            sds.append(est)
        e = np.concatenate(errs)
        e = e[np.isfinite(e)]
        bld_sd = np.mean([np.std(s) for s in sds])
        res[f"step_{0.05 * k:.2f}Hz"] = {"n_bins": len(range(0, frontend.ZOOM_N, k)), "mean_abs_err_hz": float(np.abs(e).mean()), "p95_abs_err_hz": float(np.percentile(np.abs(e), 95)),
                                        "max_abs_err_hz": float(np.abs(e).max()), "bias_hz": float(e.mean()), "within_building_sd_hz": float(bld_sd),
                                        "relative_cost_of_zoom": len(range(0, frontend.ZOOM_N, k)) / frontend.ZOOM_N}
    res["note"] = f"{len(ps)} dev buildings x floors 5/10 x 12 averages of 16 windows; error vs eigen f1 (includes the per-building bias; the within-building sd is what a relative drop sees)"
    return res


# ------------------------------------------------------------------ run
def run(small=False, cache_dir=None, out_dir=None, write=True, prime=None, workers=4, batch=12):
    t0 = time.time()
    cache_dir = cache_dir or SCRATCH / "cache"
    prior = assert_seed_blocks_disjoint()
    dn, tn = ("small_dev", "small_test") if small else ("dev", "test")
    chunk = CHUNK_SMALL if small else CHUNK
    B = 200 if small else N_BOOT
    log(f"== freq tracking small={small}: blocks {BLOCKS[dn]}, {BLOCKS[tn]} (earlier max seed {prior}; disjointness vs fusion_eval, r2b and the reserved phase ranges asserted)")
    if prime:
        nm = dn if prime == "dev" else tn
        load_block(seeds_of(nm), cache_dir, nm, workers, batch, chunk)
        log(f"== primed {nm} in {time.time() - t0:.0f}s")
        return None
    tm = {}
    t = time.time()
    Dd = load_block(seeds_of(dn), cache_dir, dn, workers, batch, chunk)
    tm["dev_block_s"] = time.time() - t
    rows, chosen, key = select(Dd)
    log(f"== chosen {chosen}")
    top = sorted(rows, key=lambda r: (r["eligible"], ) + key(r), reverse=True)[:25]
    dev_rep, _ = score(Dd, chosen, B)
    coarse = coarse_grid_study(seeds_of(dn)[:24 if not small else 6], batch)
    t = time.time()
    Dt = load_block(seeds_of(tn), cache_dir, tn, workers, batch, chunk)
    tm["test_block_s"] = time.time() - t
    test_rep, ex = score(Dt, chosen, B, with_example=True)
    out = {
        "phase": "A", "small_mode": small,
        "seed_blocks": {k: {"start": a, "n": n} for k, (a, n) in BLOCKS.items() if k in (dn, tn)}, "earlier_max_seed": prior,
        "reserved_ranges_not_used": {k: {"start": a, "n": n} for k, (a, n) in RESERVED.items()}, "seed_disjointness_asserted": True,
        "setup": {"healthy_windows": HEALTHY_NW, "healthy_hours": HEALTHY_NW * SF / 3600, "commissioning_windows_in_record": COMM_NW, "segment_windows": SEG_NW,
                  "damage_windows": DMG_NW, "A_env": A_ENV, "primary_A_env": A_ENV[IA_PRIMARY], "env_period_s": T_ENV, "storeys": STOREYS, "losses": LOSSES,
                  "cells_full_A": len(CELLS), "cells_other_A": len(CELLS_SECONDARY), "pairs": PAIRS, "M": MS, "C": CS, "delta": DELTAS, "P": PS,
                  "fa_budget_per_24h": FA_BUDGET_24H, "superframe_s": SF, "bootstrap_resamples": B, "buildings_per_block": len(seeds_of(dn))},
        "dev_table_primary": rows, "dev_table_top": top, "selected": chosen,
        "dev_scored_selected": dev_rep, "test": test_rep, "test_estimation_error": estimation_error(Dt), "dev_estimation_error": estimation_error(Dd),
        "physics_curve": physics_curve(seeds_of(dn)), "est_vs_true_test": est_vs_true(Dt, chosen), "coarse_grid_study_dev": coarse, "example": ex,
        "runtime_s": dict(tm, total=time.time() - t0),
    }
    if write:
        od = out_dir or OUT_DIR
        od.mkdir(parents=True, exist_ok=True)
        (od / "freq_tracking.json").write_text(json.dumps(out, indent=1, default=lambda o: o.tolist() if isinstance(o, np.ndarray) else o.item() if isinstance(o, np.generic) else str(o)), encoding="utf-8")
        (od / "freq_tracking.md").write_text(render_md(out), encoding="utf-8")
    log(f"== done in {time.time() - t0:.0f}s")
    return out


# ------------------------------------------------------------------ write-up
def fv(d, nd=3, pct=False):
    if d is None or d["value"] is None:
        return "n/a"
    k = 100 if pct else 1
    v = d["value"] * k
    lo, hi = d["ci95"]
    if lo is None:
        return f"{v:.{nd}f}"
    return f"{v:.{nd}f} [{lo * k:.{nd}f}, {hi * k:.{nd}f}]"


def render_md(R):
    ch = R["selected"]
    S = R["setup"]
    md = ["# Phase A: natural-frequency tracking -> DAMAGE channel", "",
          "Simulation only (`building_sim`: uniform 10-storey shear building, stationary band-limited base excitation, uniform-stiffness environmental drift as the only environment effect, no mass change). Everything runs the firmware C code (`f1track.h`, `damage.h`) through the ctypes bridge. Pre-registered rule: see the docstring of `models/training/freq_tracking_eval.py`.", "",
          f"- seeds: {R['seed_blocks']}; disjoint from every earlier block and from the reserved ranges (asserted).",
          f"- healthy record per building: {S['healthy_windows']} windows = {S['healthy_hours']:.2f} h (one window per 1.4 s superframe; level 0.5-1.5x per 128-window segment); damage recordings: the same {S['commissioning_windows_in_record']} commissioning windows then {S['damage_windows']} damaged windows; {S['buildings_per_block']} buildings per block.",
          f"- environment: uniform stiffness drift A sin(2 pi t/{S['env_period_s']:.0f} s + phi), A in {S['A_env']} (primary 0.5%); damage cells: {S['cells_full_A']} at A = 0.5%, {S['cells_other_A']} at the other A (compute budget).", "",
          "## Selection (development block, A = 0.5%, primary pair)", "",
          f"Rule: maximise mean detection over the 25 cells subject to false DAMAGE <= {S['fa_budget_per_24h']}/24 h. Top of the 300-config grid (eligible first):", "",
          "| M | C | delta % | P | false/24 h | false begins | det mean | det 5% | det 10% | det 15% | det 20% | det 30% |", "|" + "---|" * 12]
    for r in R["dev_table_top"][:20]:
        mark = " **(chosen)**" if (r["M"], r["C"], r["delta"], r["P"]) == (ch["M"], ch["C"], ch["delta"], ch["P"]) else ""
        md.append(f"| {r['M']}{mark} | {r['C']} | {100 * r['delta']:.2f} | {r['P']} | {r['fa_per_24h']:.2f}{'' if r['eligible'] else ' (over)'} | {r['false_begins']} | {r['det_mean']:.3f} | "
                  + " | ".join(f"{r['det_by_loss'][str(d)]:.2f}" for d in LOSSES) + " |")
    md += ["", f"**Chosen: M = {ch['M']}, C = {ch['C']}, delta = {100 * ch['delta']:.2f}%, P = {ch['P']}** (budget {'met' if ch['constraint_met'] else 'NOT met: lowest false rate'}; dev false {ch['fa_per_24h']:.2f}/24 h over {ch['monitoring_hours']:.0f} h, mean detection {ch['det_mean']:.3f}).", ""]
    md += ["## TEST block, scored once (95% bootstrap intervals by building)", ""]
    md += ["### False DAMAGE per 24 h of healthy monitoring", "", "| A_env | primary pair (5/10) | hours | secondary pair (3/7) | hours |", "|---|---|---|---|---|"]
    for a in S["A_env"]:
        p, s = R["test"][f"A{a * 100:g}_primary"], R["test"][f"A{a * 100:g}_secondary"]
        md.append(f"| {a * 100:g}% | {fv(p['fa_per_24h'], 2)} ({p['false_begins']} begins) | {p['monitoring_hours']:.0f} | {fv(s['fa_per_24h'], 2)} ({s['false_begins']}) | {s['monitoring_hours']:.0f} |")
    md += ["", "### Detection rate, primary pair, A = 0.5% (storey x loss; latency in superframes of 1.4 s, detected only)", "",
           "| storey | true f1 drop % at 5/10/15/20/30% loss | det 5% | det 10% | det 15% | det 20% | det 30% | latency 30% |", "|---|---|---|---|---|---|---|---|"]
    rp = R["test"]["A0.5_primary"]
    pc = R["physics_curve"]
    for s in STOREYS:
        row = pc["drop_by_storey"][str(s)]
        drops = "/".join(f"{100 * row[int(round(d / 0.05)) - 1]:.2f}" for d in LOSSES)
        md.append(f"| {s} | {drops} | " + " | ".join(f"{rp['cells'][cell_label(CELLS.index((s, d)))]['det']['value']:.2f}" for d in LOSSES)
                  + f" | {fv(rp['cells'][cell_label(CELLS.index((s, 0.30)))]['latency_sf'], 1)} |")
    md += ["", f"Mean detection over the 25 cells: **{fv(rp['det_mean'])}**. By loss: " + ", ".join(f"{float(k) * 100:g}%: {fv(v, 2)}" for k, v in rp["by_loss"].items())
           + ". By storey: " + ", ".join(f"s{k}: {fv(v, 2)}" for k, v in rp["by_storey"].items()) + ".", ""]
    md += ["### Mean detection over the common 9 cells (s in {1,5,10} x d in {10,20,30}%) per A_env and pair", "", "| A_env | primary | secondary |", "|---|---|---|"]
    for a in S["A_env"]:
        md.append(f"| {a * 100:g}% | {fv(R['test'][f'A{a * 100:g}_primary']['det_mean_common9'])} | {fv(R['test'][f'A{a * 100:g}_secondary']['det_mean_common9'])} |")
    md += ["", "Per-cell tables for the other conditions are in the JSON (`test.A*_{primary,secondary}.cells`).", ""]
    md += ["## (i) f1 estimation error vs the eigen f1, healthy windows, test block", "", "| condition | bias Hz | per-building bias sd | mean abs | median | p95 | p99 | max | within-building sd |", "|---|---|---|---|---|---|---|---|---|"]
    for k, v in R["test_estimation_error"].items():
        for tag in ("primary_pair", "secondary_pair"):
            e = v[tag]
            md.append(f"| {k} {tag[:3]} | {e['bias_hz']:+.3f} | {e['per_building_bias_sd_hz']:.3f} | {e['mean_abs_hz']:.3f} | {e['abs_q50']:.3f} | {e['abs_q95']:.3f} | {e['abs_q99']:.3f} | {e['abs_max']:.3f} | {e['within_building_sd_hz']:.3f} |")
    md += ["", "(`pri` = floors 5/10, `sec` = floors 3/7; `A*` = drift amplitude %, `M*` = averaged windows; truth = eigen f1 of the current stiffness, so the drift is NOT counted as error. Large maxima on the secondary pair: on a low floor of a low-f1 building the first mode is weak and the peak jumps to mode 2 (about 3 f1) - see the JSON per-floor window counts.)", ""]
    md += ["## (ii) Physics: eigen f1 drop vs storey stiffness loss (no estimator involved)", "",
           "For a uniform shear building the relative drop is independent of f1 (asserted over 20 buildings). Drop in % of f1:", "", "| storey | " + " | ".join(f"{100 * d:.0f}%" for d in pc["loss"]) + " |", "|---|" + "---|" * len(pc["loss"])]
    for s in range(1, 11):
        md.append(f"| {s} | " + " | ".join(f"{100 * v:.2f}" for v in pc["drop_by_storey"][str(s)]) + " |")
    md += ["", "Damage near the top barely moves f1 (the first mode has little strain energy in the top storeys): this limits ANY f1-based detector, whatever the estimator.", ""]
    md += ["## (iii) Estimated vs true drop (selected tracker, primary pair, mean over the last 80 damaged windows, test)", "",
           "| cell | true drop % (A=0.5%) | est mean % | est sd % (over buildings) | est-true rms % |", "|---|---|---|---|---|"]
    for lab, v in R["est_vs_true_test"]["A0.5"].items():
        md.append(f"| {lab} | {100 * v['true_drop_mean']:.2f} | {100 * v['est_drop_mean']:.2f} | {100 * v['est_drop_sd']:.2f} | {100 * v['est_minus_true_rms']:.2f} |")
    md += ["", "## (v) Coarser zoom grid (dev buildings, M = 16)", "", "| grid step | bins | cost vs 181 | mean abs err Hz | p95 | max | bias | within-building sd |", "|---|---|---|---|---|---|---|---|"]
    for k, v in R["coarse_grid_study_dev"].items():
        if k == "note":
            continue
        md.append(f"| {k[5:]} | {v['n_bins']} | {v['relative_cost_of_zoom']:.2f} | {v['mean_abs_err_hz']:.4f} | {v['p95_abs_err_hz']:.4f} | {v['max_abs_err_hz']:.4f} | {v['bias_hz']:+.4f} | {v['within_building_sd_hz']:.4f} |")
    md += ["", R["coarse_grid_study_dev"]["note"], "", "## Runtimes", "", f"{R['runtime_s']}", ""]
    md += ["## Limitations (honest)", "",
           "- Uniform shear building, f1 only; one mode, stationary excitation with a flat band (2-12 Hz); real ambient excitation is not white and has narrowband components (machinery, traffic) that sit on the spectrum and bias a peak-picker.",
           "- The only environment effect is a uniform stiffness drift (temperature-like); no mass change (occupancy, snow, equipment), no humidity/boundary effects, no excitation-amplitude dependence of f1 (real structures soften at higher amplitude).",
           "- Damage is a stiffness loss in one storey at once, in a building with no initial nonuniformity; the commissioned f1_0 is assumed to be taken on an undamaged structure.",
           "- The estimator bias is per building (about +-0.1 Hz); the detector works on the relative drop, so it only sees CHANGES; a slow damage that develops during the 1.02 h healthy window would be absorbed only if it happens before commissioning completes.",
           "- Packet loss is not modelled here (every slave reports every superframe); the detector holds its state with no report, and pools whatever slaves report.",
           "- The 'one window per superframe' timing assumes the ~15-20 ms spec_zoom fits the slave's ~50 ms first-poll limit; measure on hardware (docs/hardware_bringup.md).", ""]
    return chr(10).join(md) + chr(10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--small", action="store_true")
    ap.add_argument("--prime", choices=["dev", "test"], default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    out = a.out or (SCRATCH / "small_out" if a.small else None)
    run(small=a.small, cache_dir=a.cache, out_dir=out, prime=a.prime, workers=a.workers, batch=a.batch)


if __name__ == "__main__":
    main()
