"""ctypes bridge to the firmware's own spectral front-end (firmware/esp32/tools/frontend_lib.c, which includes
features.h + spectrum.h): the Python evaluation scripts run EXACTLY the C code of the sensor node.
The library is built on demand into <cache base>/frontend/ (see cachedir.py) with gcc -O2 -ffp-contract=off
and rebuilt when any source or header is newer than it.
  features5(az, gx) -> (n, 5) float32   az, gx: int16 (n, 128)
  bands(az) -> (n, 16)   zoom(az) -> (n, 181)   peak(z) -> (n,) Hz   ZOOM_F (181,) frequency grid
  build_lib(c_source, name): build any other firmware C bridge the same way (hash-named DLL, firmware flags)
"""
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import uuid
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


def build_lib(c_source, name):
    """Build a firmware C bridge (a .c file in firmware/esp32/tools/ that includes firmware headers) as a shared
    library and return its path. Reusable by other bridges (e.g. the f1 tracker or the autoencoder).
    The file name carries a hash of the C file, EVERY header in firmware/esp32/include/, this file and the compiler
    command, so an edit (or a git checkout) builds a NEW file instead of overwriting one that another process may
    have loaded, and a change of flags rebuilds too. Built to a unique temporary name, then renamed (parallel first
    builds cannot collide). Flags as the firmware: -O2 -ffp-contract=off."""
    c_source = Path(c_source).resolve()
    d = cachedir.script_dir("frontend")
    d.mkdir(parents=True, exist_ok=True)
    gcc = shutil.which("gcc")
    if gcc is None:
        raise RuntimeError("gcc not found on PATH: needed to build the firmware C bridges for Python "
                           "(install WinLibs / build-essential and retry)")
    flags = ["-O2", "-ffp-contract=off", "-shared"] + ([] if sys.platform == "win32" else ["-fPIC"])
    h = hashlib.sha256()
    for f in [c_source, *sorted((FW / "include").glob("*.h")), Path(__file__).resolve()]:
        h.update(f.name.encode())
        h.update(f.read_bytes())
    h.update(" ".join(flags).encode())
    ext = ".dll" if sys.platform == "win32" else ".so"
    dll = d / f"{name}_{h.hexdigest()[:16]}{ext}"
    if dll.exists():
        return dll
    tmp = d / f"{name}_{h.hexdigest()[:16]}_{os.getpid()}_{uuid.uuid4().hex[:8]}{ext}"
    cmd = [gcc, *flags, "-I", str(FW / "include"), str(c_source), "-o", str(tmp), "-lm"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{name} build failed:" + chr(10) + r.stderr)
    try:
        os.replace(tmp, dll)
    except OSError:  # another process won the race and has the identical file loaded: use theirs
        if not dll.exists():
            raise
        tmp.unlink(missing_ok=True)
    return dll


def _build():
    return build_lib(SOURCES[0], "frontend")


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
        assert lib.front_zoom_f0_mhz() == round(ZOOM_F0 * 1000) and lib.front_zoom_df_mhz() == round(ZOOM_DF * 1000), \
            "frontend.py zoom grid does not match spectrum.h"
        _lib = lib
    return _lib


def _i16(a):
    """int16 windows, strictly: integer-valued input within the int16 range (no silent truncation or wrap)."""
    a = np.asarray(a)
    if a.dtype != np.int16:
        if not (np.issubdtype(a.dtype, np.integer) or np.issubdtype(a.dtype, np.floating)):
            raise TypeError(f"windows must be numeric, got {a.dtype}")
        if np.issubdtype(a.dtype, np.floating) and not np.array_equal(a, np.round(a)):
            raise ValueError("windows must be integer-valued (raw int16 counts)")
        if a.size and (a.min() < -32768 or a.max() > 32767):
            raise ValueError("window values outside the int16 range")
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
    if f5 and gx is None:
        raise ValueError("features5 needs gx")
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
