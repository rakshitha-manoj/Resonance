"""Final autoencoder: train on the AE training block (healthy only), full-integer int8 post-training quantisation, Mahalanobis
baseline, numbers for the report. Tinyml venv:  python models/training/ae_final.py   (defaults = the committed model: design A, hidden 12,2,12, 30 epochs, seed 0)
Writes models/tinyml/: ae_float.keras, ae_int8.tflite, ae_norm.json (design + per-input normaliser floor), mahalanobis.json,
ae_numbers.json (everything the report quotes). Then run firmware/esp32/tools/gen_ae.py and gen_ae_vectors.py.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ae_train as T  # noqa: E402

OUT = T.ROOT / "models" / "tinyml"


def quant_np(x, scale, zp):
    t = x.astype(np.float32) / np.float32(scale)      # TFLite 2.21 reference QUANTIZE: float32 division, then round half away from zero
    a = np.abs(t)
    f = np.floor(a)
    r = np.sign(t) * (f + (a - f >= np.float32(0.5)))
    return np.clip(r.astype(np.int64) + zp, -128, 127).astype(np.int8)


def run_int8(path, z, resolver=None):
    import tensorflow as tf
    kw = {"experimental_op_resolver_type": resolver} if resolver is not None else {}
    it = tf.lite.Interpreter(model_path=str(path), **kw)
    it.allocate_tensors()
    i, o = it.get_input_details()[0], it.get_output_details()[0]
    s_in, zp_in = i["quantization"]
    s_out, zp_out = o["quantization"]
    q = quant_np(z, s_in, zp_in)
    out = np.empty_like(q)
    for k in range(len(q)):
        it.set_tensor(i["index"], q[k:k + 1])
        it.invoke()
        out[k] = it.get_tensor(o["index"])[0]
    return (out.astype(np.float32) - np.float32(zp_out)) * np.float32(s_out)


def main():
    import tensorflow as tf
    ap = argparse.ArgumentParser()
    ap.add_argument("--design", default="A")
    ap.add_argument("--hidden", default="12,2,12")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--reuse", action="store_true", help="reuse models/tinyml/ae_float.keras (skip training)")
    a = ap.parse_args()
    hidden = tuple(int(x) for x in a.hidden.split(","))
    t0 = time.time()
    tr, va = T.load("train"), T.load("val")
    comm_tr = T.make_input(tr["comm"], a.design)
    floor = (T.FLOOR_FRAC * T.unfloored_scale_median(comm_tr)).astype(np.float32)
    def zof(D, key):
        med, sc = T.node_stats(T.make_input(D["comm"], a.design), floor)
        return T.normalise(T.make_input(D[key], a.design), med, sc)
    nin = floor.size
    ztr = np.concatenate([zof(tr, k).reshape(-1, nin) for k in ("comm", "hl", "bb")])
    zva = np.concatenate([zof(va, k).reshape(-1, nin) for k in ("hl", "bb")])
    print("train windows", len(ztr), "val", len(zva), "n_in", nin, flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.reuse:
        m = tf.keras.models.load_model(OUT / "ae_float.keras")
    else:
        m = T.build_model(nin, hidden, a.seed)
        T.fit(m, ztr, zva, epochs=a.epochs, verbose=2)
        m.save(OUT / "ae_float.keras")
    rng = np.random.default_rng(1)
    rep = ztr[rng.choice(len(ztr), 3000, replace=False)]
    conv = tf.lite.TFLiteConverter.from_keras_model(m)
    conv.optimizations = [tf.lite.Optimize.DEFAULT]
    conv.representative_dataset = lambda: ([r[None].astype(np.float32)] for r in rep)
    conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    conv.inference_input_type = tf.int8
    conv.inference_output_type = tf.int8
    blob = conv.convert()
    (OUT / "ae_int8.tflite").write_bytes(blob)
    (OUT / "ae_norm.json").write_text(json.dumps({"design": a.design, "floor_frac": T.FLOOR_FRAC, "floor": [float(x) for x in floor], "n_in": int(nin)}, indent=1))
    # ---- float vs int8 on validation healthy windows
    sub = zva[rng.choice(len(zva), 20000, replace=False)]
    yf = m.predict(sub, batch_size=4096, verbose=0)
    yq = run_int8(OUT / "ae_int8.tflite", sub)
    mf, mq = ((sub - yf) ** 2).mean(1), ((sub - yq) ** 2).mean(1)
    # ---- Mahalanobis baseline (shrinkage chosen on validation recall at 0.5% healthy FPR, like the AE design search)
    mu = ztr.mean(0).astype(np.float64)
    cov = np.cov(ztr.astype(np.float64).T)
    avg = np.trace(cov) / nin
    best = None
    for lam in (0.0, 0.02, 0.1, 0.3, 0.6, 0.8, 0.95):
        S = (1 - lam) * cov + lam * avg * np.eye(nin)
        P = np.linalg.inv(S)
        d2 = lambda z, P=P: np.einsum("...i,ij,...j->...", z - mu, P, z - mu)  # noqa: E731
        rec = mah_recall(d2, zof, va)
        print("mahalanobis shrinkage", lam, "recall@0.5%% %.3f" % rec["mean"], flush=True)
        if best is None or rec["mean"] > best[0]:
            best = (rec["mean"], lam, P, rec)
    _, lam, P, mrec = best
    (OUT / "mahalanobis.json").write_text(json.dumps({"shrinkage": lam, "design": a.design, "mean": mu.tolist(), "precision": P.tolist(),
                                                      "n_train_windows": int(len(ztr)), "val_recall_at_0.5pct": mrec}, indent=1))
    # ---- AE reference recall on validation: float vs int8 (python quantisation, TFLite reference kernels)
    num = {"design": a.design, "hidden": list(hidden), "n_in": int(nin), "params": int(m.count_params()), "int8_tflite_bytes": len(blob),
           "float_recon_mse_val_healthy": float(mf.mean()), "int8_recon_mse_val_healthy": float(mq.mean()),
           "recon_mse_median_float": float(np.median(mf)), "recon_mse_median_int8": float(np.median(mq)),
           "score_rel_diff_mean": float(np.mean(np.abs(mq - mf) / np.maximum(mf, 1e-6))),
           "score_corr": float(np.corrcoef(mf, mq)[0, 1]), "mahalanobis_shrinkage": lam, "train_seconds": round(time.time() - t0)}
    for tag, fn in (("float", lambda z: ((z - m.predict(z.reshape(-1, nin), batch_size=8192, verbose=0).reshape(z.shape)) ** 2).mean(-1)),
                    ("int8", lambda z: ((z - run_int8(OUT / "ae_int8.tflite", z.reshape(-1, nin)).reshape(z.shape)) ** 2).mean(-1))):
        num["val_recall_" + tag] = mah_recall(None, zof, va, score=fn)
    (OUT / "ae_numbers.json").write_text(json.dumps(num, indent=1))
    print(json.dumps(num, indent=1))


def mah_recall(d2, zof, va, score=None, fpr=0.005):
    """Window-level recall at the healthy FPR (as ae_design_search), generic score function of z (nb,10,nw,n_in) -> (nb,10,nw)."""
    sc = score or d2
    def ratio(key):
        z = zof(va, key)
        return sc(z)
    cz = zof(va, "comm")
    ref = np.maximum(np.median(sc(cz), axis=-1), 1e-9)[..., None]
    hl = ratio("hl") / ref
    thr = float(np.percentile(hl, 100 * (1 - fpr)))
    rec = {}
    for kind in ("imp", "sp", "shift"):
        hit = tot = 0
        for f in (5, 10):
            r = (ratio(f"{kind}{f}") / ref)[:, f - 1]
            mk = va[f"{kind}{f}_mask"]
            hit += int(((r >= thr) & mk).sum())
            tot += int(mk.sum())
        rec[kind] = hit / max(tot, 1)
    r3 = (ratio("x3") / ref)[:, [4, 9]]
    rec["x3"] = float((r3 >= thr).mean())
    rec["mean"] = float(np.mean([rec[k] for k in ("imp", "sp", "shift", "x3")]))
    rec["threshold"] = thr
    return rec


if __name__ == "__main__":
    main()
