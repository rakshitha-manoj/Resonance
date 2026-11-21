"""Training / validation data of the autoencoder (Phase B): HEALTHY simulated recordings, seed block
6,300,000-6,300,399 (train) and 6,300,400-6,300,499 (validation), all 10 floors, per-window base20 features computed by
the node's C code (ae_bridge.base20). Only the validation block also gets anomalous recordings (impact / sparse impact /
frequency shift / x3 building-wide, floors 5 and 10): used ONLY to compare input designs and architectures, never to
train. The dev block (6,100,000+240) and the reserved fresh test block (6,400,000+) are not touched here.

Per building (exc ids below do not collide with fusion_eval(_r2b): 0,1,2,3,4,5,11,12,100+f,200+f,300,301,400+f):
  comm  exc 50   20 windows, default excitation, level 1.0     (the commissioning windows of every node)
  hl    exc 51   100 windows, default excitation 2-12 Hz, level multiplier U(0.5,1.5) (fusion_eval.level_mult)
  bb    exc 52   100 windows, broadband excitation 2-45 Hz, level multiplier U(0.5,1.5)
  validation only: imp<f> exc 150+f, shift<f> exc 250+f, sp<f> exc 450+f, x3 exc 53 (amp 3.0), 30 windows each, floors 5 and 10.
Output arrays (nb, 10, nwin, 20) float32. Cache: <cache>/ae_data/ (cachedir.py), chunks of 20 buildings, parallel.
"""
import hashlib
import inspect
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import fusion_eval as fe  # noqa: E402
import fusion_eval_r2b as r2b  # noqa: E402
import ae_bridge  # noqa: E402
import cachedir  # noqa: E402

bs = fe.bs
N = bs.N
SCRATCH = cachedir.script_dir("ae_data")
BLOCKS_AE = {"train": (6_300_000, 400), "val": (6_300_400, 100)}
EXC = {"comm": 50, "hl": 51, "bb": 52, "x3": 53}
NW = {"comm": 20, "hl": 100, "bb": 100}
NW_ANOM = 30
ANOM_FLOORS = (5, 10)
CHUNK = 20


def assert_disjoint():
    """Seed ranges of this block must not intersect ANY earlier block (train_building, fusion_eval R2, R2b incl. their
    small blocks), the reserved fresh test block 6,400,000-6,400,999 or the other agent's 6,500,000-6,699,999."""
    import train_building as tb
    earlier = [(a, a + n) for a, n in tb.BLOCKS.values()] + [(a, a + n) for a, n in fe.BLOCKS.values()] +               [(a, a + n) for a, n in r2b.BLOCKS2.values()] + [(0, fe.prior_max_seed())]
    reserved = [(6_400_000, 6_401_000), (6_500_000, 6_700_000)]
    own = sorted((a, a + n) for a, n in BLOCKS_AE.values())
    assert own[0][1] <= own[1][0], "train / val blocks overlap"
    for a, b in own:
        for c, d in earlier + reserved:
            assert b <= c or a >= d, f"AE block {(a, b)} intersects {(c, d)}"
    used_exc = set(EXC.values()) | {150 + f for f in ANOM_FLOORS} | {250 + f for f in ANOM_FLOORS} | {450 + f for f in ANOM_FLOORS}
    r2_exc = set(r2b.EXC.values()) | set(fe.EXC.values()) | {100 + f for f in fe.FLOORS} | {200 + f for f in fe.FLOORS} | {400 + f for f in fe.FLOORS}
    assert not (used_exc & r2_exc), used_exc & r2_exc
    return fe.prior_max_seed()


def seeds_of(name):
    a, n = BLOCKS_AE[name]
    return list(range(a, a + n))


def _windows_base(az, gx):
    """az, gx int16 (10, T) -> (10, nw, 20) base features from the C code."""
    aw, gw = bs.to_windows(az), bs.to_windows(gx)
    nf, nw = aw.shape[:2]
    b = ae_bridge.base20(aw.reshape(-1, N), gw.reshape(-1, N))
    return b.reshape(nf, nw, ae_bridge.NBASE)


def sim_chunk(seeds, anomalies):
    ps = [bs.building_params(s) for s in seeds]
    nb = len(ps)
    out = {"seeds": np.array(seeds)}

    def record(exc, nwin, excitation="default", amp=1.0, mult=None, kind=None, floor=None):
        sig = bs.simulate_many(ps, exc, nwin, None, excitation, amp, batch=5)
        if mult is not None:
            sig = sig * mult[:, None, None]
        res = np.empty((nb, bs.NFLOOR, nwin, ae_bridge.NBASE), np.float32)
        mask = np.zeros((nb, nwin), dtype=bool)
        for i, p in enumerate(ps):
            kw, s_i = {}, sig[i]
            if kind == "imp":
                kw["impact_floor"] = floor
            elif kind == "shift":
                kw["shift_floor"] = floor
            elif kind == "sp":
                sp, mk = r2b.sparse_spike(p["seed"], exc, floor, nwin * N)
                s_i = s_i.copy()
                s_i[floor - 1] += sp
                mask[i] = mk
            az, gx, tr = bs.sensor_output(p, s_i, exc, return_truth=True, **kw)
            res[i] = _windows_base(az, gx)
            m = tr["impact_mask"] if kind == "imp" else tr["shift_mask"] if kind == "shift" else None
            if m is not None:
                mask[i] = m
        return res, mask

    out["comm"], _ = record(EXC["comm"], NW["comm"])
    for nm, ex in (("hl", "default"), ("bb", "broadband")):
        m = np.array([fe.level_mult(p["seed"], EXC[nm]) for p in ps])
        out[nm], _ = record(EXC[nm], NW[nm], excitation=ex, mult=m)
    if anomalies:
        for kind, base in (("imp", 150), ("shift", 250), ("sp", 450)):
            for f in ANOM_FLOORS:
                exc = base + f
                m = np.array([fe.level_mult(p["seed"], exc) for p in ps])
                out[f"{kind}{f}"], out[f"{kind}{f}_mask"] = record(exc, NW_ANOM, mult=m, kind=kind, floor=f)
        out["x3"], _ = record(EXC["x3"], NW_ANOM, amp=3.0)
    return out


def chunk_key(seeds, anomalies):
    h = hashlib.sha256()
    for f in (Path(bs.__file__), ae_bridge.SRC, ae_bridge.FW / "include" / "ae_input.h", Path(__file__)):
        h.update(f.read_bytes())
    h.update(inspect.getsource(r2b.sparse_spike).encode())
    h.update(json.dumps({"seeds": [int(s) for s in seeds], "exc": EXC, "nw": NW, "anom": anomalies, "v": 1}, sort_keys=True).encode())
    return h.hexdigest()


def _job(args):
    sd, anomalies, f, key = args
    z = sim_chunk(sd, anomalies)
    tmp = f.with_suffix(".tmp.npz")
    np.savez(tmp, key=np.array(key), **z)
    tmp.replace(f)
    return f


def load_block(name, cache_dir=None, workers=5):
    """-> dict of arrays concatenated over buildings."""
    assert_disjoint()
    cache_dir = Path(cache_dir) if cache_dir else SCRATCH / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    seeds = seeds_of(name)
    anomalies = name == "val"
    jobs, files = [], []
    for i in range(0, len(seeds), CHUNK):
        sd = seeds[i:i + CHUNK]
        key = chunk_key(sd, anomalies)
        f = cache_dir / f"ae_{name}_{sd[0]}_{len(sd)}_{key[:16]}.npz"
        files.append((f, key, sd))
        ok = False
        if f.exists():
            try:
                z = np.load(f)
                ok = str(z["key"]) == key
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            jobs.append((sd, anomalies, f, key))
    if jobs:
        t = time.time()
        fe.log(f"  [ae {name}] simulating {len(jobs)} chunks on {workers} workers")
        with ProcessPoolExecutor(workers) as ex:
            list(ex.map(_job, jobs))
        fe.log(f"  [ae {name}] done in {time.time() - t:.0f}s")
    parts = []
    for f, key, sd in files:
        z = dict(np.load(f))
        assert str(z.pop("key")) == key and np.array_equal(z["seeds"], sd)
        parts.append(z)
    return {k: np.concatenate([p[k] for p in parts], axis=0) for k in parts[0]}


if __name__ == "__main__":
    print("disjointness ok, earlier max seed", assert_disjoint())
    for nm in ("train", "val"):
        D = load_block(nm)
        print(nm, {k: v.shape for k, v in D.items()})
