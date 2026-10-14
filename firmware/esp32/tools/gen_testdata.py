import json
import struct
from pathlib import Path
import joblib
import numpy as np
import pandas as pd

root = Path(__file__).resolve().parents[3]
fw = root / "firmware/esp32"
exp = json.loads((root / "models/selected/model_export.json").read_text())
t = exp["trees"][0]
used = exp["used_feature_names"]
df = pd.read_csv(root / "data/processed/dataset.csv")
model = joblib.load(root / "models/selected/model.joblib")
pred = model.predict(df[exp["input_features"]].values.astype(np.float64)).astype(np.int32)
az = df[[f"az_{i}" for i in range(128)]].values.astype(np.int16)
gx = df[[f"gx_{i}" for i in range(128)]].values.astype(np.int16)
feat = df[used].values.astype(np.float64)
n = len(df)

with open(fw / "test/data/windows.bin", "wb") as f:
    for i in range(n):
        f.write(az[i].astype("<i2").tobytes())
        f.write(gx[i].astype("<i2").tobytes())
        f.write(struct.pack("<i", int(pred[i])))
        f.write(feat[i].astype("<f8").tobytes())
print(f"windows.bin: {n} records x 556 B little-endian: az int16[128], gx int16[128], class int32, features float64[5] ({', '.join(used)})")

thr = {t["feature_name"][i]: t["threshold"][i] for i in range(t["n_nodes"]) if t["feature_name"][i]}
sel = {}
for j, name in enumerate(used):
    if name.endswith("domfreq"):
        hit = np.abs(feat[:, j] - thr[name]) <= 1.5 * 0.78125
        sel[f"{name} within 1 bin of threshold"] = np.where(hit)[0]
    else:
        hit = np.abs(feat[:, j] - thr[name]) <= 0.01 * abs(thr[name])
        sel[f"{name} within 1% of threshold"] = np.where(hit)[0]

for name, ax in (("az", az), ("gx", gx)):
    x = ax.astype(np.float64)
    mag = np.abs(np.fft.rfft(x - x.mean(axis=1, keepdims=True), axis=1))[:, 1:]
    top = np.sort(mag, axis=1)[:, -2:]
    hit = (top[:, 1] - top[:, 0]) <= 1e-3 * top[:, 1]
    sel[f"{name} top-two spectral bins within 0.1%"] = np.where(hit)[0]

chosen = set()
for name, idx in sel.items():
    new = set(idx.tolist()) - chosen
    print(f"criterion '{name}': {len(idx)} windows, {len(new)} new")
    chosen |= new
print(f"boundary windows total: {len(chosen)}")

target = 500
rng = np.random.default_rng(12345)
rec = df["recording_id"].values
per_class = max(30, (target - len(chosen)) // 3)
added = {}
for c in range(3):
    pool = np.array([i for i in np.where(pred == c)[0] if i not in chosen])
    take = min(per_class, len(pool))
    pick = rng.choice(pool, size=take, replace=False)
    chosen |= set(pick.tolist())
    added[c] = (take, len(set(rec[pick])))
for c, (take, nrec) in added.items():
    print(f"random fill class {c}: {take} windows from {nrec} recordings")
order = np.array(sorted(chosen))
print(f"selftest windows: {len(order)}, per class: {np.bincount(pred[order], minlength=3).tolist()}")

def arr(a):
    return "{" + ",".join(str(int(v)) for v in a) + "}"

lines = ["#pragma once", "#include <stdint.h>", f"#define ST_N {len(order)}"]
lines.append("static const int16_t st_az[ST_N][128] = {" + ",\n".join(arr(az[i]) for i in order) + "};")
lines.append("static const int16_t st_gx[ST_N][128] = {" + ",\n".join(arr(gx[i]) for i in order) + "};")
lines.append("static const uint8_t st_cls[ST_N] = " + arr(pred[order]) + ";")
lines.append("static const float st_feat[ST_N][5] = {" + ",\n".join("{" + ",".join(float.hex(float(np.float32(v))) + "f" for v in feat[i]) + "}" for i in order) + "};")
(fw / "include/selftest_data.h").write_text("\n".join(lines) + "\n")
print("wrote selftest_data.h")
