"""ctypes bridge to the firmware's autoencoder path (firmware/esp32/tools/ae_lib.c: ae_input.h + ae.h), built with
frontend.build_lib (firmware flags). Evaluation runs exactly the node's C:
  base20(az, gx)                         -> (n, 20) float32   16 az log band-powers, az_std, az_kurt, gx_std, gx_kurt
  (later sections) commissioning normaliser -> int8 AE -> score, see the functions below.
"""
import ctypes
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frontend  # noqa: E402

FW = frontend.FW
SRC = FW / "tools" / "ae_lib.c"
NBASE = 20
_lib = None


def _load():
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(str(frontend.build_lib(SRC, "ae")))
        vp = ctypes.c_void_p
        lib.ae_base_batch.argtypes = [ctypes.c_int, vp, vp, vp]
        lib.ae_base_batch.restype = None
        _init_lib2(lib)
        _lib = lib
    return _lib


def lib():
    return _load()


def base20(az, gx):
    az = frontend._i16(az)
    gx = frontend._i16(gx)
    if az.shape != gx.shape:
        raise ValueError("az and gx must have the same number of windows")
    out = np.zeros((az.shape[0], NBASE), np.float32)
    _load().ae_base_batch(az.shape[0], az.ctypes.data, gx.ctypes.data, out.ctypes.data)
    return out


def _init_lib2(lib):
    vp = ctypes.c_void_p
    lib.ae_eval_node.argtypes = [vp, ctypes.c_int, vp, vp, vp, vp]
    lib.ae_eval_node.restype = None
    lib.ae_quant_batch.argtypes = [ctypes.c_int, vp, vp]
    lib.ae_quant_batch.restype = None
    lib.ae_run_batch.argtypes = [ctypes.c_int, vp, vp]
    lib.ae_run_batch.restype = None
    lib.ae_lib_n_in.restype = ctypes.c_int
    lib.ae_lib_comm_n.restype = ctypes.c_int


def n_in():
    return _load().ae_lib_n_in()


def eval_node(comm_b20, b20):
    """One node: commission on 20 base windows (20, 20), then score the windows b20 (n, 20).
    -> ratio (n,) float32, z (n, n_in) float32 normalised inputs, ref (median commissioning score)."""
    lib = _load()
    comm = np.ascontiguousarray(comm_b20, np.float32)
    b = np.ascontiguousarray(b20, np.float32)
    assert comm.shape == (lib.ae_lib_comm_n(), NBASE) and b.ndim == 2 and b.shape[1] == NBASE
    n = b.shape[0]
    ratio = np.zeros(n, np.float32)
    z = np.zeros((n, lib.ae_lib_n_in()), np.float32)
    ref = np.zeros(1, np.float32)
    lib.ae_eval_node(comm.ctypes.data, n, b.ctypes.data, ratio.ctypes.data, z.ctypes.data, ref.ctypes.data)
    return ratio, z, float(ref[0])


def quantise(x):
    x = np.ascontiguousarray(x, np.float32).reshape(-1, n_in())
    q = np.zeros(x.shape, np.int8)
    _load().ae_quant_batch(x.shape[0], x.ctypes.data, q.ctypes.data)
    return q


def run_int8(q):
    q = np.ascontiguousarray(q, np.int8).reshape(-1, n_in())
    out = np.zeros(q.shape, np.int8)
    _load().ae_run_batch(q.shape[0], q.ctypes.data, out.ctypes.data)
    return out
