"""Write the AE parity vectors: test/data/ae_vectors.bin and include/ae_selftest_vectors.h.
Expected outputs come from tf.lite.Interpreter running models/tinyml/ae_int8.tflite with the REFERENCE kernels
(OpResolverType.BUILTIN_REF); the default (optimised) resolver is also run and any disagreement with the reference is
reported. Input vectors: int8 vectors only need the kernel; the first N_FLOAT vectors also carry the float32 model input
(normalised space) whose q_in comes from a REAL TFLite QUANTIZE op (single-op model with the AE's input scale / zero point,
BUILTIN_REF resolver: round half away from zero of the float32 division val / scale, + zp, clamp), not from a Python mirror.
The numpy mirror (quantise) is checked against that op on 1e6 near-half values; the default (XNNPACK) QUANTIZE kernel rounds
half to even and is NOT the reference. Out-of-range inputs (NaN / inf / |v/scale| > int32) are undefined behaviour in the TFLite
kernel and are excluded from the vectors (C maps NaN to -128, see ae.h).
Mix: real normalised validation windows (healthy, impacts, shifts, x3; seeds 6,300,400+), random full-range int8, extremes.
Layout (bytes): "AEV1", int32 n_vec, int32 n_in, int32 n_float; n_vec x (int8 q_in[n_in], int8 q_out[n_in]);
n_float x float32 x[n_in].
Run (tinyml venv):  python firmware/esp32/tools/gen_ae_vectors.py
"""
import json
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "models" / "training"))
sys.dont_write_bytecode = True
import ae_train as T  # noqa: E402

TFLITE = ROOT / "models" / "tinyml" / "ae_int8.tflite"
N_VEC, N_FLOAT, N_SELF = 10000, 1000, 64
OUT_BIN = ROOT / "firmware" / "esp32" / "test" / "data" / "ae_vectors.bin"
OUT_H = ROOT / "firmware" / "esp32" / "include" / "ae_selftest_vectors.h"


def interp(resolver):
    import tensorflow as tf
    kw = {"experimental_op_resolver_type": resolver} if resolver is not None else {}
    it = tf.lite.Interpreter(model_path=str(TFLITE), **kw)
    it.allocate_tensors()
    return it


def run(it, q):
    i, o = it.get_input_details()[0], it.get_output_details()[0]
    out = np.empty_like(q)
    for k in range(len(q)):
        it.set_tensor(i["index"], q[k:k + 1])
        it.invoke()
        out[k] = it.get_tensor(o["index"])[0]
    return out


def quantise(x, scale, zp):
    """numpy mirror of TFLite reference QUANTIZE (float32 division, round half away from zero); checked against the real op in main()."""
    t = x.astype(np.float32) / np.float32(scale)
    a = np.abs(t)
    f = np.floor(a)
    r = np.sign(t) * (f + (a - f >= np.float32(0.5)))      # std::round: half away from zero, exact
    return np.clip(r.astype(np.int64) + zp, -128, 127).astype(np.int8)


def quantize_op_model(scale, zp, n_in):
    """Flatbuffer of a one-op TFLite model: float32 input -> QUANTIZE -> int8 output with (scale, zp)."""
    import flatbuffers
    from tensorflow.lite.python import schema_py_generated as S
    m = S.ModelT()
    m.version = 3
    oc = S.OperatorCodeT()
    oc.builtinCode = oc.deprecatedBuiltinCode = S.BuiltinOperator.QUANTIZE
    oc.version = 1
    m.operatorCodes = [oc]
    m.buffers = [S.BufferT()]
    t0, t1 = S.TensorT(), S.TensorT()
    t0.shape, t0.type, t0.buffer, t0.name = [1, n_in], S.TensorType.FLOAT32, 0, b"x"
    t1.shape, t1.type, t1.buffer, t1.name = [1, n_in], S.TensorType.INT8, 0, b"q"
    q = S.QuantizationParametersT()
    q.scale, q.zeroPoint = [float(scale)], [int(zp)]
    t1.quantization = q
    op = S.OperatorT()
    op.opcodeIndex, op.inputs, op.outputs = 0, [0], [1]
    sg = S.SubGraphT()
    sg.tensors, sg.inputs, sg.outputs, sg.operators, sg.name = [t0, t1], [0], [1], [op], b"main"
    m.subgraphs = [sg]
    fb = flatbuffers.Builder(1024)
    fb.Finish(m.Pack(fb), b"TFL3")
    return bytes(fb.Output())


def tflite_quantize(x, scale, zp, resolver):
    """Quantise float rows x (n, n_in) with the real TFLite QUANTIZE op."""
    import tensorflow as tf
    it = tf.lite.Interpreter(model_content=quantize_op_model(scale, zp, x.shape[1]), experimental_op_resolver_type=resolver)
    it.allocate_tensors()
    i, o = it.get_input_details()[0], it.get_output_details()[0]
    out = np.empty(x.shape, np.int8)
    for k in range(len(x)):
        it.set_tensor(i["index"], x[k:k + 1].astype(np.float32))
        it.invoke()
        out[k] = it.get_tensor(o["index"])[0]
    return out


def near_half(rng, scale, shape):
    """float32 values within a few ulps of (k + 0.5) * scale, where v * (1/scale) and v / scale can round differently."""
    ks = rng.integers(-130, 130, shape).astype(np.float32)
    v = ((ks + np.float32(0.5)) * np.float32(scale)).astype(np.float32)
    for _ in range(6):                                     # random walk of 0..6 ulps in a random direction each
        step = rng.integers(-1, 2, shape)
        v = np.where(step > 0, np.nextafter(v, np.float32(1e9)), np.where(step < 0, np.nextafter(v, np.float32(-1e9)), v)).astype(np.float32)
    return v


def main():
    import tensorflow as tf
    norm = json.loads((ROOT / "models" / "tinyml" / "ae_norm.json").read_text())
    design, floor = norm["design"], np.array(norm["floor"], np.float32)
    it_ref = interp(tf.lite.experimental.OpResolverType.BUILTIN_REF)
    d = it_ref.get_input_details()[0]
    scale, zp = d["quantization"]
    nin = d["shape"][-1]
    rng = np.random.default_rng(2026)
    va = T.load("val")
    zs = []
    for key in ("hl", "bb", "imp5", "sp5", "shift5", "shift10", "x3"):
        med, sc = T.node_stats(T.make_input(va["comm"], design), floor)
        z = T.normalise(T.make_input(va[key], design), med, sc)
        zs.append(z.reshape(-1, nin))
    pool = np.concatenate(zs)
    # float vectors: real windows + boundary cases
    real = pool[rng.choice(len(pool), 550, replace=False)]
    half = near_half(rng, scale, (300, nin))
    ext = rng.choice(np.array([-1e4, -50, -9, 9, 50, 1e4, 0], np.float32), (150, nin))
    xf = np.concatenate([real, half, ext]).astype(np.float32)[:N_FLOAT]
    q_float = tflite_quantize(xf, scale, zp, tf.lite.experimental.OpResolverType.BUILTIN_REF)
    assert (q_float == quantise(xf, scale, zp)).all(), "numpy mirror differs from the TFLite QUANTIZE op"
    # mirror vs the real op on a large near-half set; and how often the old v * (1/scale) form would have differed
    big = near_half(rng, scale, (50000, nin))
    qb = tflite_quantize(big, scale, zp, tf.lite.experimental.OpResolverType.BUILTIN_REF)
    mirror_bad = int((qb != quantise(big, scale, zp)).sum())
    t_old = big * (np.float32(1) / np.float32(scale))
    f_old = np.floor(np.abs(t_old))
    old = np.clip((np.sign(t_old) * (f_old + (np.abs(t_old) - f_old >= np.float32(0.5)))).astype(np.int64) + zp, -128, 127).astype(np.int8)
    print(f"QUANTIZE op vs numpy mirror: {mirror_bad} differences of {qb.size} near-half values; the old v*(1/scale) form would differ in {int((qb != old).sum())}")
    assert mirror_bad == 0
    n_rest = N_VEC - len(xf)
    real_q = quantise(pool[rng.choice(len(pool), n_rest // 2, replace=False)], scale, zp)
    rand_q = rng.integers(-128, 128, (n_rest - len(real_q) - 600, nin)).astype(np.int8)
    ext_q = rng.choice(np.array([-128, -127, 0, 126, 127, zp], np.int8), (600, nin))
    qin = np.concatenate([q_float, real_q, rand_q, ext_q]).astype(np.int8)
    assert qin.shape == (N_VEC, nin)
    qref = run(it_ref, qin)
    qdef = run(interp(None), qin)
    diff = int((qref != qdef).sum())
    print(f"reference vs default resolver: {diff} differing int8 outputs of {qref.size}")
    OUT_BIN.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_BIN, "wb") as f:
        f.write(b"AEV1" + struct.pack("<3i", N_VEC, nin, len(xf)))
        for i in range(N_VEC):
            f.write(qin[i].tobytes() + qref[i].tobytes())
        f.write(xf.astype("<f4").tobytes())
    print(f"wrote {OUT_BIN} ({OUT_BIN.stat().st_size} bytes), {N_VEC} vectors, {len(xf)} with float inputs")
    # embedded set for the on-device selftest: 64 vectors (first 32 real windows, then extremes)
    sel = np.concatenate([np.arange(0, 48), np.arange(N_VEC - 16, N_VEC)])[:N_SELF]
    lines = ["#pragma once", "/* GENERATED by tools/gen_ae_vectors.py: int8 parity vectors (TFLite reference kernels) for src/selftest.cpp */",
             "#include <stdint.h>", f"#define AE_ST_N {len(sel)}", f"static const int8_t ae_st_in[AE_ST_N][{nin}] = {{"]
    lines += ["    {" + ", ".join(str(int(v)) for v in qin[i]) + "}," for i in sel] + ["};", f"static const int8_t ae_st_out[AE_ST_N][{nin}] = {{"]
    lines += ["    {" + ", ".join(str(int(v)) for v in qref[i]) + "}," for i in sel] + ["};", ""]
    OUT_H.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {OUT_H}")


if __name__ == "__main__":
    main()
