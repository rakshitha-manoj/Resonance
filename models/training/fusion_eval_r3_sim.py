"""R3 simulation + cache: exactly the recordings of fusion_eval_r2b.simulate_chunk (same seeds, exc ids, level multipliers,
impact / sparse / shift generation), but per window it stores
  <rec>_std   (nb, 10, nw) float32  az_std from localisation_gate.window_features, all floors (what rule b uses; identical to R2b's)
  <rec>_b20   (nb, 4, nw, 20) float32  AE base features from the node's C code at the slave floors 3, 5, 7, 10
so rule b is reproduced bit for bit and the AE / Mahalanobis scores are recomputed without re-simulating.
Cache: <cache>/fusion_eval_r3/cache (cachedir.py). Chunks of 20 buildings simulated in parallel worker processes.
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

bs, lg = fe.bs, fe.lg
N = bs.N
SCRATCH = cachedir.script_dir("fusion_eval_r3")
FLOORS = fe.FLOORS          # (3, 5, 7, 10): the slave floors of both pairs
CHUNK = 20
EXC, COMM_NW, NW, HL_NW, NZ_NW = r2b.EXC, r2b.COMM_NW, r2b.NW, r2b.HL_NW, r2b.NZ_NW
SIM_VERSION = 1


def sim_chunk(seeds):
    ps = [bs.building_params(s) for s in seeds]
    nb = len(ps)
    out = {"seeds": np.array(seeds)}
    fidx = [f - 1 for f in FLOORS]

    def record(name, exc, nwin, amp=1.0, zero=False, mult=None, kind=None, floors=None, batch=5):
        sig = np.zeros((nb, bs.NFLOOR, nwin * N)) if zero else bs.simulate_many(ps, exc, nwin, None, "default", amp, batch=batch)
        if mult is not None:
            sig = sig * mult[:, None, None]
        std = np.empty((nb, bs.NFLOOR, nwin), dtype=np.float32)
        b20 = np.empty((nb, len(FLOORS), nwin, ae_bridge.NBASE), dtype=np.float32)
        mask = np.zeros((nb, nwin), dtype=bool)
        for i, p in enumerate(ps):
            kw, s_i = {}, sig[i]
            if kind == "imp":
                kw["impact_floor"] = int(floors[i])
            elif kind == "shift":
                kw["shift_floor"] = int(floors[i])
            elif kind == "sp":
                sp, mk = r2b.sparse_spike(p["seed"], exc, int(floors[i]), nwin * N)
                s_i = s_i.copy()
                s_i[int(floors[i]) - 1] += sp
                mask[i] = mk
            az, gx, tr = bs.sensor_output(p, s_i, exc, return_truth=True, **kw)
            a_std, _, _ = lg.window_features(bs.to_windows(az))
            std[i] = a_std.astype(np.float32)
            aw, gw = bs.to_windows(az[fidx]), bs.to_windows(gx[fidx])
            b20[i] = ae_bridge.base20(aw.reshape(-1, N), gw.reshape(-1, N)).reshape(len(FLOORS), nwin, ae_bridge.NBASE)
            m = tr["impact_mask"] if kind == "imp" else tr["shift_mask"] if kind == "shift" else None
            if m is not None:
                mask[i] = m
        out[name + "_std"], out[name + "_b20"] = std, b20
        if kind in ("imp", "shift", "sp"):
            out[name + "_mask"] = mask

    record("comm", EXC["comm"], COMM_NW)
    mh = np.array([fe.level_mult(p["seed"], EXC["hl"]) for p in ps])
    record("hl", EXC["hl"], HL_NW, mult=mh)
    record("nz", EXC["nz"], NZ_NW, zero=True)
    for nm, a in r2b.STRONG.items():
        record(nm, EXC[nm], NW, amp=a)
    for kind, base_exc in (("imp", 100), ("sp", 400), ("shift", 200)):
        for f in FLOORS:
            exc = base_exc + f
            m = np.array([fe.level_mult(p["seed"], exc) for p in ps])
            record(f"{kind}{f}", exc, NW, mult=m, kind=kind, floors=np.full(nb, f))
    for kind, nm in (("imp", "uimp"), ("shift", "ushift")):
        m = np.array([fe.level_mult(p["seed"], EXC[nm]) for p in ps])
        fl = np.array([fe.uninstr_floor(p["seed"], kind) for p in ps])
        record(nm, EXC[nm], NW, mult=m, kind=kind, floors=fl)
        out[nm + "_floor"] = fl
    return out


def chunk_key(seeds):
    h = hashlib.sha256()
    for f in (Path(bs.__file__), ae_bridge.SRC, ae_bridge.FW / "include" / "ae_input.h", Path(__file__)):
        h.update(f.read_bytes())
    h.update(inspect.getsource(lg.window_features).encode())
    h.update(inspect.getsource(r2b.sparse_spike).encode())
    h.update(json.dumps({"seeds": [int(s) for s in seeds], "nw": NW, "hl": HL_NW, "nz": NZ_NW, "comm": COMM_NW, "exc": EXC, "floors": FLOORS,
                         "unf": fe.UNINSTR_FLOORS, "strong": r2b.STRONG, "v": SIM_VERSION}, sort_keys=True).encode())
    return h.hexdigest()


def _job(args):
    sd, f, key = args
    z = sim_chunk(sd)
    tmp = f.with_suffix(".tmp.npz")
    np.savez(tmp, key=np.array(key), **z)
    tmp.replace(f)
    return str(f)


def load_block(seeds, tag, cache_dir=None, workers=5):
    cache_dir = Path(cache_dir) if cache_dir else SCRATCH / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    jobs, files = [], []
    for i in range(0, len(seeds), CHUNK):
        sd = seeds[i:i + CHUNK]
        key = chunk_key(sd)
        f = cache_dir / f"r3_{tag}_{sd[0]}_{len(sd)}_{key[:16]}.npz"
        files.append((f, key, sd))
        ok = False
        if f.exists():
            try:
                ok = str(np.load(f)["key"]) == key
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            jobs.append((sd, f, key))
    if jobs:
        t = time.time()
        fe.log(f"  [r3 {tag}] simulating {len(jobs)} of {len(files)} chunks on {workers} workers")
        with ProcessPoolExecutor(workers) as ex:
            list(ex.map(_job, jobs))
        fe.log(f"  [r3 {tag}] simulated in {time.time() - t:.0f}s")
    parts = []
    for f, key, sd in files:
        z = dict(np.load(f))
        assert str(z.pop("key")) == key and np.array_equal(z["seeds"], sd)
        parts.append(z)
    return {k: np.concatenate([p[k] for p in parts], axis=0) for k in parts[0]}


if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "dev"
    assert tag in ("dev", "small_dev"), "only development blocks are simulated by this entry point"
    a, n = r2b.BLOCKS2[tag]
    D = load_block(list(range(a, a + n)), tag)
    print({k: v.shape for k, v in list(D.items())[:6]})
