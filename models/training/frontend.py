"""ctypes bridge to the firmware's own spectral front-end (firmware/esp32/tools/frontend_lib.c, which includes
features.h + spectrum.h): the Python evaluation scripts run EXACTLY the C code of the sensor node.
The library is built on demand into <cache base>/frontend/ (see cachedir.py) with gcc -O2 -ffp-contract=off
and rebuilt when any source or header is newer than it.
  features5(az, gx) -> (n, 5) float32   az, gx: int16 (n, 128)
  bands(az) -> (n, 16)   zoom(az) -> (n, 181)   peak(z) -> (n,) Hz   ZOOM_F (181,) frequency grid
"""
import ctypes
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cachedir  # noqa: E402

FW = cachedir.ROOT / "firmware" / "esp32"
SOURCES = [FW / "tools" / "frontend_lib.c", FW / "include" / "features.h", FW / "include" / "spectrum.h",
           FW / "include" / "spectrum_tables.h", FW / "include" / "model.h"]
WIN_N, ZOOM_N, NB = 128, 181, 16
ZOOM_F0, ZOOM_DF = 3.0, 0.05
ZOOM_F = ZOOM_F0 + ZOOM_DF * np.arange(ZOOM_N)
_lib = None


def _build():
    d = cachedir.script_dir("frontend")
    d.mkdir(parents=True, exist_ok=True)
    dll = d / ("frontend.dll" if sys.platform == "win32" else "frontend.so")
    if dll.exists() and all(dll.stat().st_mtime >= s.stat().st_mtime for s in SOURCES):
        return dll
    gcc = shutil.which("gcc")
    if gcc is None:
        raise RuntimeError("gcc not found on PATH: needed to build the firmware front-end for Python "
                           "(install WinLibs / build-essential and retry)")
    cmd = [gcc, "-O2", "-ffp-contract=off", "-shared", "-I", str(FW / "include"), str(SOURCES[0]), "-o", str(dll), "-lm"]
    if sys.platform != "win32":
        cmd.insert(4, "-fPIC")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("front-end build failed:\n" + r.stderr)
    return dll


def _load():
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(str(_build()))
        vp = ctypes.c_void_p
        lib.front_batch.argtypes = [ctypes.c_int, vp, vp, vp, vp, vp]
        lib.front_batch.restype = None
        lib.front_peak.argtypes = [ctypes.c_int, vp, vp]
        lib.front_peak.restype = None
        assert lib.front_zoom_n() == ZOOM_N
        _lib = lib
    return _lib


def _i16(a):
    a = np.ascontiguousarray(a, dtype=np.int16)
    if a.ndim == 1:
        a = a[None]
    if a.shape[-1] != WIN_N:
        raise ValueError("windows must have 128 samples")
    return a.reshape(-1, WIN_N)


def _p(a):
    return None if a is None else a.ctypes.data


def _run(az, gx, f5, bands, zoom):
    az = _i16(az)
    n = az.shape[0]
    gx = _i16(gx) if gx is not None else None
    if gx is not None and gx.shape[0] != n:
        raise ValueError("az and gx must have the same number of windows")
    out = {}
    if f5:
        out["f5"] = np.zeros((n, 5), np.float32)
    if bands:
        out["b"] = np.zeros((n, NB), np.float32)
    if zoom:
        out["z"] = np.zeros((n, ZOOM_N), np.float32)
    _load().front_batch(n, _p(az), _p(gx), _p(out.get("f5")), _p(out.get("b")), _p(out.get("z")))
    return out


def features5(az, gx):
    """The 5 used features [az_std, az_kurt, az_domfreq, gx_std, gx_domfreq] as float32, exactly features()."""
    return _run(az, gx, True, False, False)["f5"]


def bands(az):
    return _run(az, None, False, True, False)["b"]


def zoom(az):
    return _run(az, None, False, False, True)["z"]


def peak(z):
    z = np.ascontiguousarray(z, dtype=np.float32)
    z = z.reshape(-1, ZOOM_N)
    out = np.zeros(z.shape[0], np.float32)
    _load().front_peak(z.shape[0], _p(z), _p(out))
    return out
