import itertools
import json
import struct
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeClassifier

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

def smooth(raw):
    hist = [-1, -1, -1]
    state = 0
    out = []
    for r in raw:
        hist = hist[1:] + [int(r)]
        for c in range(3):
            if hist.count(c) >= 2:
                state = c
                break
        out.append(state)
    return out

seqs = []
for k in range(1, 7):
    for combo in itertools.product(range(3), repeat=k):
        seqs.append(list(combo))
n_exh = len(seqs)
y = df["label"].map({"normal": 0, "abnormal": 1, "faulty": 2}).values.astype(int)
ids = list(dict.fromkeys(rec.tolist()))
rows = {r: np.where(rec == r)[0] for r in ids}
assert all(len(v) == 40 and (np.diff(v) == 1).all() for v in rows.values())
rec_class = {r: int(y[rows[r][0]]) for r in ids}
all_ids = {c: np.array(sorted(r for r in ids if rec_class[r] == c)) for c in range(3)}
X = df[exp["input_features"]].values.astype(np.float64)
for rep in range(10):
    rr = np.random.default_rng(42 + 8000 + rep)
    fids = [set() for _ in range(5)]
    for c in range(3):
        perm = rr.permutation(all_ids[c])
        for k in range(5):
            fids[k] |= {int(i) for i in perm[k * 10:(k + 1) * 10]}
    oof = np.full(n, -1)
    for k in range(5):
        te = np.isin(rec, list(fids[k]))
        oof[te] = DecisionTreeClassifier(max_depth=3, random_state=42).fit(X[~te], y[~te]).predict(X[te])
    assert (oof >= 0).all()
    for r in all_ids[0]:
        seqs.append(oof[rows[r]].tolist())
    faults = [r for c in (1, 2) for r in all_ids[c]]
    for j, r in enumerate(faults):
        seqs.append(np.concatenate([oof[rows[all_ids[0][j % 50]]][-10:], oof[rows[r]], oof[rows[all_ids[0][(j + 25) % 50]]][:10]]).tolist())
with open(fw / "test/data/smooth.bin", "wb") as f:
    f.write(struct.pack("<I", len(seqs)))
    for s in seqs:
        f.write(struct.pack("<H", len(s)))
        f.write(np.array(s, dtype=np.int8).tobytes())
        f.write(np.array(smooth(s), dtype=np.int8).tobytes())
print(f"smooth.bin: {len(seqs)} sequences ({n_exh} exhaustive k=1..6, {len(seqs) - n_exh} out-of-fold from 10 repeats), layout little-endian: uint32 count, then per sequence uint16 len, int8 raw[len], int8 state[len]; state starts normal, history empty")

seg_len = 20
err_rec = None
for r in ids:
    v = rows[r]
    for i in range(1, 39):
        if pred[v[i]] != y[v[i]] and pred[v[i - 1]] == y[v[i]] and pred[v[i + 1]] == y[v[i]]:
            err_rec = (r, i)
            break
    if err_rec:
        break
assert err_rec
iso_r, iso_i = err_rec
iso_start = min(max(iso_i - 10, 0), 40 - seg_len)

def clean_start(c, skip=()):
    for r in all_ids[c]:
        v = rows[r]
        if r not in skip and (pred[v[:seg_len]] == c).all():
            return r
    raise SystemExit("no clean recording")

rn = clean_start(0)
rn2 = clean_start(0, (rn,))
rf = clean_start(2)
ra = clean_start(1)
segs = [(rn, 0), (rf, 0), (rn, 20), (ra, 0), (rn2, 0), (iso_r, iso_start)]
sel_rows, seg_start = [], []
for r, s0 in segs:
    seg_start.append(len(sel_rows))
    sel_rows += rows[r][s0:s0 + seg_len].tolist()
sel_rows = np.array(sel_rows)
sq_label = y[sel_rows]
sq_raw = pred[sel_rows]
sq_state = smooth(sq_raw)
seg_label = [int(sq_label[s]) for s in seg_start]
for k, (r, s0) in enumerate(segs):
    print(f"scripted segment {k}: recording {r} class {seg_label[k]} windows {s0}..{s0 + seg_len - 1}, start index {seg_start[k]}")
print(f"isolated raw error at recording {iso_r} window {iso_i} (sequence index {seg_start[-1] + iso_i - iso_start})")
print("scripted raw  :", "".join(str(int(v)) for v in sq_raw))
print("scripted label:", "".join(str(int(v)) for v in sq_label))
print("scripted state:", "".join(str(int(v)) for v in sq_state))
lines.append(f"#define SQ_N {len(sel_rows)}")
lines.append(f"#define SQ_NSEG {len(segs)}")
lines.append("static const int16_t sq_az[SQ_N][128] = {" + ",\n".join(arr(az[i]) for i in sel_rows) + "};")
lines.append("static const int16_t sq_gx[SQ_N][128] = {" + ",\n".join(arr(gx[i]) for i in sel_rows) + "};")
lines.append("static const uint8_t sq_label[SQ_N] = " + arr(sq_label) + ";")
lines.append("static const uint8_t sq_raw[SQ_N] = " + arr(sq_raw) + ";")
lines.append("static const uint8_t sq_state[SQ_N] = " + arr(sq_state) + ";")
lines.append("static const uint16_t sq_seg_start[SQ_NSEG] = " + arr(seg_start) + ";")
(fw / "include/selftest_data.h").write_text("\n".join(lines) + "\n")
print("wrote selftest_data.h")
