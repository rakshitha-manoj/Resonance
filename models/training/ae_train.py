"""Autoencoder training helpers (TensorFlow venv: D:\\Resonance\\.venv-tinyml). Numpy-only data loading (no building_sim /
sklearn import needed): reads the chunk caches written by ae_data.py (run that first in the main venv).

Input designs (all computed on the node from base20 = [16 az log band-powers, az_std, az_kurt, gx_std, gx_kurt]):
  A  the 20 base features as they are
  B  shape only: 16 log band-powers minus their mean (removes the overall level), az_kurt, gx_kurt          (18)
  C  B + log(gx_std+1) - log(az_std+1)                                                                    (19)
  D  base20 but log(std+1) instead of std                                                                   (20)
Per-node normalisation (on device, over the 20 commissioning windows): z = (x - med) / max(1.4826 * MAD, floor_j),
median of 20 = mean of the two middle values, in float32. floor_j is a per-input constant (see FLOOR_FRAC).
"""
import glob
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.dont_write_bytecode = True
CACHE = Path(__import__("os").environ.get("RESONANCE_CACHE_DIR", ROOT / ".cache")) / "ae_data" / "cache"
BLOCKS_AE = {"train": (6_300_000, 400), "val": (6_300_400, 100)}
FLOOR_FRAC = 0.25      # floor_j = FLOOR_FRAC * median over training nodes of the unfloored scale_j
MAD_K = np.float32(1.4826)


def load(name):
    """Cached base20 arrays of a block (chunks sorted by first seed; the full seed range is asserted)."""
    a, n = BLOCKS_AE[name]
    parts = []
    for f in sorted(glob.glob(str(CACHE / f"ae_{name}_*.npz"))):
        z = dict(np.load(f))
        z.pop("key", None)
        parts.append((int(z["seeds"][0]), z))
    best = {}
    for s0, z in parts:  # several cache versions may exist: keep the newest file per chunk start
        best[s0] = z
    seeds = np.concatenate([best[s]["seeds"] for s in sorted(best)])
    assert np.array_equal(seeds, np.arange(a, a + n)), "incomplete cache: run ae_data.py"
    return {k: np.concatenate([best[s][k] for s in sorted(best)], axis=0) for k in best[a] if k != "seeds"}


# ------------------------------------------------------------------ designs
def make_input(b, design):
    """b (..., 20) float32 -> (..., n_in) float32 (numpy float32 arithmetic)."""
    b = np.asarray(b, np.float32)
    bands = b[..., :16]
    if design == "A":
        return b.copy()
    if design == "D":
        o = b.copy()
        o[..., 16] = np.log(b[..., 16] + np.float32(1))
        o[..., 18] = np.log(b[..., 18] + np.float32(1))
        return o
    shape = bands - bands.mean(-1, keepdims=True, dtype=np.float32)
    out = [shape, b[..., 17:18], b[..., 19:20]]
    if design == "C":
        out.append((np.log(b[..., 18] + np.float32(1)) - np.log(b[..., 16] + np.float32(1)))[..., None])
    return np.concatenate(out, axis=-1).astype(np.float32)


def median_f32(x, axis):
    s = np.sort(x, axis=axis)
    n = x.shape[axis]
    lo = np.take(s, n // 2 - 1, axis=axis)
    hi = np.take(s, n // 2, axis=axis)
    return ((lo + hi) * np.float32(0.5)).astype(np.float32) if n % 2 == 0 else np.take(s, n // 2, axis=axis)


def node_stats(comm_x, floor):
    """comm_x (..., 20, n_in) -> med, scale (..., n_in)."""
    med = median_f32(comm_x, -2)
    mad = median_f32(np.abs(comm_x - med[..., None, :]), -2)
    scale = np.maximum(MAD_K * mad, floor).astype(np.float32)
    return med, scale


def unfloored_scale_median(comm_x):
    med = median_f32(comm_x, -2)
    mad = median_f32(np.abs(comm_x - med[..., None, :]), -2)
    s = (MAD_K * mad).reshape(-1, comm_x.shape[-1])
    return np.median(s, axis=0).astype(np.float32)


def normalise(x, med, scale):
    return ((x - med[..., None, :]) / scale[..., None, :]).astype(np.float32)


# ------------------------------------------------------------------ model
def build_model(n_in, hidden=(12, 4, 12), seed=0):
    import tensorflow as tf
    tf.keras.utils.set_random_seed(seed)
    L = tf.keras.layers
    m = [L.Input((n_in,))]
    for h in hidden:
        m.append(L.Dense(h, activation="relu"))
    m.append(L.Dense(n_in))
    return tf.keras.Sequential(m)


def fit(model, z, zval=None, epochs=20, batch=512, lr=2e-3, verbose=0):
    import tensorflow as tf
    model.compile(tf.keras.optimizers.Adam(lr), loss="mse")
    cb = [tf.keras.callbacks.ReduceLROnPlateau(factor=0.5, patience=2, min_lr=1e-5)]
    return model.fit(z, z, epochs=epochs, batch_size=batch, shuffle=True, validation_data=(zval, zval) if zval is not None else None,
                     verbose=verbose, callbacks=cb)


def mse_rows(model, z):
    y = model.predict(z.reshape(-1, z.shape[-1]), batch_size=8192, verbose=0)
    return ((z.reshape(-1, z.shape[-1]) - y) ** 2).mean(1).reshape(z.shape[:-1])
