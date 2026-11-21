"""ctypes bridge to the firmware's SHM tracker and detector (firmware/esp32/tools/shm_lib.c, which includes
f1track.h + damage.h + spectrum.h), built with frontend.build_lib (firmware flags, hash-named DLL).
  track(z, m, c) -> dict(est, valid, drop, f1_0, wcomm)   z: (n, 181) float32 zoom spectra of ONE slave, in time order
  gate(est_raw, valid_raw, c) -> same dict, on stored raw estimates (commissioning consistency + band gate)
  detect(drop, ok, delta, p, start) -> (n_begin, first_begin, active_at_start, state)
        drop/ok: (nseq, n, ns) pooled-over-ns-slaves inputs of dmg_update, per superframe
  peak_grid(z, f0, df) -> spec_peak on an arbitrary grid (coarser-grid study)
Python runs EXACTLY the C code of the node (and of the master for the detector)."""
import ctypes
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frontend  # noqa: E402

SRC = frontend.FW / "tools" / "shm_lib.c"
ZOOM_N = frontend.ZOOM_N
_lib = None


def _load():
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(str(frontend.build_lib(SRC, "shm")))
        vp = ctypes.c_void_p
        lib.shm_track.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, vp, vp, vp, vp, vp, vp, vp, vp]
        lib.shm_track.restype = ctypes.c_int
        lib.shm_gate.argtypes = [ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_int, vp, vp, vp, vp, vp, vp, vp]
        lib.shm_gate.restype = ctypes.c_int
        lib.shm_detect.argtypes = [ctypes.c_float, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, vp, vp,
                                   ctypes.c_int, vp, vp, vp, vp]
        lib.shm_detect.restype = None
        lib.shm_peak_grid.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_float, ctypes.c_float, vp, vp]
        lib.shm_peak_grid.restype = None
        assert lib.shm_zoom_n() == ZOOM_N
        _lib = lib
    return _lib


def limits():
    lib = _load()
    return {"ring_max": lib.shm_ring_max(), "c_max": lib.shm_c_max()}


def _p(a):
    return None if a is None else a.ctypes.data


def track(z, m, c):
    z = np.ascontiguousarray(z, dtype=np.float32)
    if z.ndim != 2 or z.shape[1] != ZOOM_N:
        raise ValueError("z must be (n, 181)")
    n = z.shape[0]
    est = np.empty(n, np.float32)
    valid = np.zeros(n, np.uint8)
    drop = np.empty(n, np.float32)
    f10 = np.zeros(1, np.float32)
    wc = np.zeros(1, np.int32)
    est_raw = np.empty(n, np.float32)
    valid_raw = np.zeros(n, np.uint8)
    if _load().shm_track(int(m), int(c), n, _p(z), _p(est), _p(valid), _p(drop), _p(f10), _p(wc), _p(est_raw), _p(valid_raw)) != 0:
        raise ValueError(f"bad tracker size m={m} c={c}")
    return {"est": est, "valid": valid.astype(bool), "drop": drop, "f1_0": float(f10[0]), "wcomm": int(wc[0]),
            "est_raw": est_raw, "valid_raw": valid_raw.astype(bool)}


def gate(est_raw, valid_raw, c, spread=-1.0, band=-1.0):
    """Commissioning + band gating (f1t_feed) on stored raw estimates of ONE slave. Same outputs as track()."""
    est_raw = np.ascontiguousarray(est_raw, dtype=np.float32)
    valid_raw = np.ascontiguousarray(valid_raw, dtype=np.uint8)
    n = est_raw.shape[0]
    est = np.empty(n, np.float32)
    valid = np.zeros(n, np.uint8)
    drop = np.empty(n, np.float32)
    f10 = np.zeros(1, np.float32)
    wc = np.zeros(1, np.int32)
    if _load().shm_gate(int(c), float(spread), float(band), n, _p(est_raw), _p(valid_raw), _p(est), _p(valid), _p(drop), _p(f10), _p(wc)) != 0:
        raise ValueError(f"bad commissioning size c={c}")
    return {"est": est, "valid": valid.astype(bool), "drop": drop, "f1_0": float(f10[0]), "wcomm": int(wc[0])}


def detect(drop, ok, delta, p, start=0, want_state=False):
    drop = np.ascontiguousarray(drop, dtype=np.float32)
    ok = np.ascontiguousarray(ok, dtype=np.uint8)
    if drop.ndim == 2:
        drop, ok = drop[None], ok[None]
    assert drop.shape == ok.shape and drop.ndim == 3
    nseq, n, ns = drop.shape
    nb = np.zeros(nseq, np.int32)
    fb = np.zeros(nseq, np.int32)
    act = np.zeros(nseq, np.int32)
    st = np.zeros((nseq, n), np.uint8) if want_state else None
    _load().shm_detect(float(delta), int(p), nseq, n, ns, _p(drop), _p(ok), int(start), _p(nb), _p(fb), _p(act), _p(st))
    return nb, fb, act, st


def peak_grid(z, f0, df):
    z = np.ascontiguousarray(z, dtype=np.float32)
    z = z.reshape(-1, z.shape[-1])
    out = np.zeros(z.shape[0], np.float32)
    _load().shm_peak_grid(z.shape[0], z.shape[1], float(f0), float(df), _p(z), _p(out))
    return out
