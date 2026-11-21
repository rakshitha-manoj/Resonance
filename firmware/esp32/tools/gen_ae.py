"""Generate firmware/esp32/include/ae_model.h from the int8 TFLite autoencoder (models/tinyml/ae_int8.tflite) and
models/tinyml/ae_norm.json (per-input normaliser floors, input design name).

The model is parsed straight from the flatbuffer (TensorFlow's generated schema), so weights, biases, quantisation
parameters and the fused activations are exactly what the TFLite interpreter executes. Output multipliers/shifts are
derived the way TFLite's FullyConnected prepare does (real = in_scale * w_scale / out_scale in double, QuantizeMultiplier =
frexp + round(q * 2^31)). Per-tensor or per-output-channel weight scales are both handled.

Run (tinyml venv, needs TensorFlow for the schema):  python firmware/esp32/tools/gen_ae.py
"""
import hashlib
import json
import math
import os
import sys
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
TFLITE = ROOT / "models" / "tinyml" / "ae_int8.tflite"
NORM = ROOT / "models" / "tinyml" / "ae_norm.json"
OUT = ROOT / "firmware" / "esp32" / "include" / "ae_model.h"


def hexf(x):
    x = float(np.float32(x))
    if x == 0.0:
        return "0.0f"
    if not math.isfinite(x):
        raise ValueError("non-finite constant")
    return float.hex(x).replace("0x1.0p", "0x1p") + "f"


def quantize_multiplier(real):
    """TFLite QuantizeMultiplier (double -> int32 multiplier, shift)."""
    if real == 0.0:
        return 0, 0
    q, shift = math.frexp(real)
    q_fixed = int(math.floor(q * (1 << 31) + 0.5))   # TfLiteRound = round half away from zero (q > 0)
    if q_fixed == (1 << 31):
        q_fixed //= 2
        shift += 1
    if shift < -31:
        shift, q_fixed = 0, 0
    if shift > 30:
        shift, q_fixed = 30, (1 << 31) - 1
    return q_fixed, shift


def parse(path):
    from tensorflow.lite.python import schema_py_generated as S
    buf = Path(path).read_bytes()
    m = S.ModelT.InitFromObj(S.Model.GetRootAsModel(buf, 0))
    assert len(m.subgraphs) == 1
    sg = m.subgraphs[0]
    names = {v: k for k, v in vars(S.BuiltinOperator).items() if isinstance(v, int)}
    ops = []
    for op in sg.operators:
        oc = m.operatorCodes[op.opcodeIndex]
        code = max(oc.builtinCode, oc.deprecatedBuiltinCode)
        ops.append((names.get(code, str(code)), op))
    return m, sg, ops, hashlib.sha256(buf).hexdigest(), S


def tensor_q(m, t):
    q = t.quantization
    sc = np.array(q.scale if q is not None and q.scale is not None else [], dtype=np.float32)
    zp = np.array(q.zeroPoint if q is not None and q.zeroPoint is not None else [], dtype=np.int64)
    return sc, zp


def tensor_data(m, t, dtype):
    d = m.buffers[t.buffer].data
    return np.frombuffer(bytes(bytearray(d)), dtype=dtype).reshape(t.shape)


def build():
    m, sg, ops, digest, S = parse(TFLITE)
    assert all(n == "FULLY_CONNECTED" for n, _ in ops), [n for n, _ in ops]
    assert len(sg.inputs) == 1 and len(sg.outputs) == 1
    cur = int(sg.inputs[0])
    t_in = sg.tensors[cur]
    assert t_in.type == S.TensorType.INT8
    in_sc, in_zp = tensor_q(m, t_in)
    assert in_sc.size == 1 and in_zp.size == 1
    layers = []
    cur_sc, cur_zp = float(in_sc[0]), int(in_zp[0])
    for i, (n, op) in enumerate(ops):
        assert int(op.inputs[0]) == cur, "ops are not a simple chain"
        w, b = sg.tensors[int(op.inputs[1])], sg.tensors[int(op.inputs[2])]
        o = sg.tensors[int(op.outputs[0])]
        W = tensor_data(m, w, np.int8)
        B = tensor_data(m, b, np.int32)
        wsc, wzp = tensor_q(m, w)
        bias_sc, bias_zp = tensor_q(m, b)
        assert not np.any(wzp), "weights must be symmetric (zero point 0)"
        osc, ozp = tensor_q(m, o)
        assert osc.size == 1 and ozp.size == 1
        out_sc, out_zp = float(osc[0]), int(ozp[0])
        n_out, n_in = W.shape
        wsc = np.broadcast_to(wsc, (n_out,)) if wsc.size in (1, n_out) else None
        assert wsc is not None, "unexpected weight scale shape"
        mult, shift = [], []
        for c in range(n_out):
            real = float(np.float32(cur_sc)) * float(wsc[c]) / float(np.float32(out_sc))   # in double, as TFLite
            mm, ss = quantize_multiplier(real)
            mult.append(mm)
            shift.append(ss)
            bsc = cur_sc * float(wsc[c])                      # TFLite requires bias scale == input scale * weight scale (zero point 0)
            assert abs(float(bias_sc[c if bias_sc.size > 1 else 0]) - bsc) <= 1e-6 * bsc, "bias scale != input scale * weight scale"
            assert not np.any(bias_zp), "bias zero point must be 0"
        act = op.builtinOptions.fusedActivationFunction
        qmin, qmax = -128, 127
        if act == S.ActivationFunctionType.RELU:
            amin, amax = max(qmin, out_zp), qmax        # TFLite CalculateActivationRangeQuantized: relu lower bound = quantised 0
        else:
            assert act == S.ActivationFunctionType.NONE, act
            amin, amax = qmin, qmax
        layers.append(dict(n_in=int(n_in), n_out=int(n_out), W=W, B=B, mult=mult, shift=shift, in_off=-cur_zp, out_off=out_zp,
                           amin=amin, amax=amax, relu=act == S.ActivationFunctionType.RELU))
        cur = int(op.outputs[0])
        cur_sc, cur_zp = out_sc, out_zp
    assert cur == int(sg.outputs[0])
    return dict(layers=layers, in_scale=float(in_sc[0]), in_zp=int(in_zp[0]), out_scale=cur_sc, out_zp=cur_zp, sha=digest)


def arr(vals, per=12):
    return ",\n    ".join(", ".join(str(int(v)) for v in vals[i:i + per]) for i in range(0, len(vals), per))


def main():
    mdl = build()
    norm = json.loads(NORM.read_text())
    L = mdl["layers"]
    n_in = L[0]["n_in"]
    assert L[-1]["n_out"] == n_in and len(norm["floor"]) == n_in
    o = ["#pragma once",
         "/* GENERATED by tools/gen_ae.py from models/tinyml/ae_int8.tflite + ae_norm.json: do not edit. */",
         "#include <stdint.h>", "",
         f"/* source model sha256 {mdl['sha']} ; input design {norm['design']} */",
         f"#define AE_N_IN {n_in}", f"#define AE_N_LAYERS {len(L)}", f"#define AE_MAX_DIM {max(max(l['n_in'], l['n_out']) for l in L)}",
         f"#define AE_IN_ZP {mdl['in_zp']}", f"#define AE_OUT_ZP {mdl['out_zp']}",
         f"static const float AE_IN_SCALE = {hexf(mdl['in_scale'])};   /* {float(np.float32(mdl['in_scale'])):.9g} */",
         f"static const float AE_OUT_SCALE = {hexf(mdl['out_scale'])};   /* {float(np.float32(mdl['out_scale'])):.9g} */",
         f"static const float AE_NORM_FLOOR[{n_in}] = {{", "    " + ", ".join(hexf(v) for v in norm["floor"]), "};", ""]
    for i, l in enumerate(L):
        o += [f"/* layer {i}: {l['n_in']} -> {l['n_out']}{' + ReLU' if l['relu'] else ''}; weights [out][in] */",
              f"static const int8_t ae_w{i}[{l['n_out'] * l['n_in']}] = {{", "    " + arr(l["W"].reshape(-1)), "};",
              f"static const int32_t ae_b{i}[{l['n_out']}] = {{", "    " + arr(l["B"]), "};",
              f"static const int32_t ae_m{i}[{l['n_out']}] = {{", "    " + arr(l["mult"]), "};",
              f"static const int8_t ae_s{i}[{l['n_out']}] = {{", "    " + arr(l["shift"]), "};", ""]
    o += ["typedef struct {", "    int n_in, n_out;", "    const int8_t *w;", "    const int32_t *bias, *mult;", "    const int8_t *shift;",
          "    int32_t in_off, out_off, act_min, act_max;", "} AeLayer;", "",
          "static const AeLayer AE_LAYERS[AE_N_LAYERS] = {"]
    for i, l in enumerate(L):
        o.append(f"    {{{l['n_in']}, {l['n_out']}, ae_w{i}, ae_b{i}, ae_m{i}, ae_s{i}, {l['in_off']}, {l['out_off']}, {l['amin']}, {l['amax']}}},")
    o += ["};", ""]
    OUT.write_text("\n".join(o), encoding="utf-8", newline="\n")
    params = sum(l["n_in"] * l["n_out"] + l["n_out"] for l in L)
    print(f"wrote {OUT}: {len(L)} layers, {params} parameters, input scale {mdl['in_scale']:.6g} zp {mdl['in_zp']}, output scale {mdl['out_scale']:.6g} zp {mdl['out_zp']}")


if __name__ == "__main__":
    main()
