import itertools
from pathlib import Path
import numpy as np
import pandas as pd

FS = 100
N = 128
SEED = 42
RECORDINGS_PER_CLASS = 50
WINDOWS_PER_RECORDING = 40
CLASSES = ["normal", "abnormal", "faulty"]
AXES = ["ax", "ay", "az", "gx", "gy", "gz"]
BASELINE = np.array([0, 0, 16384, 0, 0, 0])
OUT = Path(__file__).resolve().parents[1] / "processed" / "dataset.csv"

rng = np.random.default_rng(SEED)
T = N * WINDOWS_PER_RECORDING
t = np.arange(T) / FS


def make_recording(label):
    f0 = rng.uniform(4, 12)
    amp = rng.uniform(150, 500)
    harm = rng.uniform(0, 0.25) * rng.integers(0, 2)
    env = np.ones(T)
    fdrift = np.zeros(T)
    dc = np.zeros(T)
    spikes = np.zeros(T)
    if label == "abnormal":
        amp = rng.uniform(380, 1200)
        env = 1 + rng.uniform(0, 1.2) * t / t[-1]
        fdrift = rng.uniform(-1.0, 1.0) * t / t[-1]
        dc = rng.uniform(-150, 150) * t / t[-1]
    if label == "faulty":
        shift = rng.random() < 0.6
        spiky = rng.random() < 0.7 or not shift
        amp = rng.uniform(150, 600)
        if shift:
            f0 = rng.uniform(14, 30)
        if spiky:
            rate = rng.uniform(0.3 if shift else 0.8, 2.0)
            n_sp = rng.poisson(rate * T / FS)
            tau = rng.uniform(0.08, 0.25)
            for s in rng.integers(0, T, n_sp):
                tt = np.arange(T - s) / FS
                spikes[s:] += rng.choice([-1, 1]) * rng.uniform(1500, 4500) * np.exp(-tt / tau) * np.sin(2 * np.pi * f0 * tt + rng.uniform(0, 2 * np.pi))
    phase = 2 * np.pi * np.cumsum(f0 + fdrift) / FS
    base = env * amp * (np.sin(phase) + harm * np.sin(2 * phase + rng.uniform(0, 2 * np.pi)))
    sig = np.zeros((T, 6))
    for a in range(6):
        if a == 2:
            gain, shift_ph, sigma = 1.0, 0.0, rng.uniform(20, 60)
        elif a < 2:
            gain, shift_ph, sigma = rng.uniform(0.03, 0.25), rng.uniform(0, 2 * np.pi), rng.uniform(20, 60)
        else:
            gain, shift_ph, sigma = rng.uniform(0.02, 0.2), rng.uniform(0, 2 * np.pi), rng.uniform(5, 20)
        shifted = env * amp * (np.sin(phase + shift_ph) + harm * np.sin(2 * phase + shift_ph))
        sig[:, a] = gain * (base if a == 2 else shifted)
        sig[:, a] += gain * spikes + rng.normal(0, sigma, T) + dc * (a == 2)
        sig[:, a] += rng.normal(0, 40) if a < 3 else rng.normal(0, 15)
    return np.clip(np.rint(sig + BASELINE), -32768, 32767).astype(np.int16)


def features(w):
    x = w.astype(np.float64)
    mu = x.mean(axis=1, keepdims=True)
    xc = x - mu
    var = (xc ** 2).mean(axis=1)
    rms = np.sqrt((x ** 2).mean(axis=1))
    std = np.sqrt(var)
    kurt = np.where(var > 0, (xc ** 4).mean(axis=1) / np.where(var > 0, var, 1) ** 2 - 3, 0.0)
    mag = np.abs(np.fft.rfft(xc, axis=1))
    dom = (np.argmax(mag[:, 1:], axis=1) + 1) * FS / N
    return rms, std, kurt, dom


rows_raw, labels, rec_ids = [], [], []
rec_id = 0
for label in CLASSES:
    for _ in range(RECORDINGS_PER_CLASS):
        rec = make_recording(label)
        wins = rec.reshape(WINDOWS_PER_RECORDING, N, 6).transpose(0, 2, 1).reshape(WINDOWS_PER_RECORDING, 6 * N)
        rows_raw.append(wins)
        labels += [label] * WINDOWS_PER_RECORDING
        rec_ids += [rec_id] * WINDOWS_PER_RECORDING
        rec_id += 1

raw = np.vstack(rows_raw)
raw_cols = [f"{a}_{i}" for a in AXES for i in range(N)]
df = pd.DataFrame(raw, columns=raw_cols)
windows = raw.reshape(-1, 6, N)
feat_cols = []
for ai, a in enumerate(AXES):
    for name, vals in zip(["rms", "std", "kurt", "domfreq"], features(windows[:, ai, :])):
        df[f"{a}_{name}"] = vals
        feat_cols.append(f"{a}_{name}")
df.insert(0, "label", labels)
df.insert(0, "recording_id", rec_ids)

OUT.parent.mkdir(parents=True, exist_ok=True)
df.to_csv(OUT, index=False)

print("total windows:", len(df))
print("windows per class:", df.groupby("label").size().reindex(CLASSES).to_dict())
print("recordings per class:", df.groupby("label")["recording_id"].nunique().reindex(CLASSES).to_dict())
print("raw min/max:", raw.min(), raw.max())
print("features NaN:", int(df[feat_cols].isna().sum().sum()), "inf:", int(np.isinf(df[feat_cols].to_numpy()).sum()))
pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 50)
g = df.groupby("label")
print("\naz features per class (mean / std)")
az = [c for c in feat_cols if c.startswith("az_")]
print(pd.concat([g[az].mean().reindex(CLASSES).add_suffix("_mean"), g[az].std().reindex(CLASSES).add_suffix("_std")], axis=1).round(2).T)
print("\nall features mean per class")
print(g[feat_cols].mean().reindex(CLASSES).round(2).T)
print("\nall features std per class")
print(g[feat_cols].std().reindex(CLASSES).round(2).T)

y = df["label"].map({c: i for i, c in enumerate(CLASSES)}).to_numpy()
best = []
for c in feat_cols:
    x = df[c].to_numpy()
    edges = np.unique(np.quantile(x, np.linspace(0, 1, 101)))[1:-1]
    cnt = np.array([[np.sum((y == k) & (x <= e)) for e in edges] for k in range(3)])
    tot = np.bincount(y, minlength=3)
    cnt = np.concatenate([cnt, tot[:, None]], axis=1)
    acc = 0
    for i, j in itertools.combinations(range(len(edges) + 1), 2):
        reg = np.stack([cnt[:, i], cnt[:, j] - cnt[:, i], tot - cnt[:, j]], axis=1)
        for p in itertools.permutations(range(3)):
            acc = max(acc, sum(reg[k, p[k]] for k in range(3)) / len(y))
    best.append((acc, c))
best.sort(reverse=True)
print("\nbest single-feature 3-class threshold accuracy (top 8)")
for acc, c in best[:8]:
    print(f"{c:12s} {acc:.3f}")
