import json, time
from pathlib import Path
import numpy as np, pandas as pd
from joblib import Parallel, delayed
from sklearn.ensemble import RandomForestClassifier
from sklearn.tree import DecisionTreeClassifier

SEED = 42; THRESH = 0.90
ROOT = Path(__file__).resolve().parents[2]
OUTD = ROOT / "models" / "selected" / "comparison"
OUTD.mkdir(parents=True, exist_ok=True)
AXES = ["ax", "ay", "az", "gx", "gy", "gz"]
cols18 = [f"{a}_{f}" for a in AXES for f in ["std", "kurt", "domfreq"]]
df = pd.read_csv(ROOT / "data" / "processed" / "dataset.csv")
X = df[cols18].to_numpy(dtype=np.float64)
y = df["label"].map({"normal": 0, "abnormal": 1, "faulty": 2}).to_numpy()
rec = df["recording_id"].to_numpy()
rec_class = {int(r): int(c) for r, c in zip(rec, y)}
all_ids = {c: np.array(sorted(r for r, k in rec_class.items() if k == c)) for c in range(3)}
assert len(rec_class) == 150

configs = [(f"dt_d{d}", "tree", d, None) for d in [3,4,5,6,7,8]]
configs += [(f"rf_n{n}_d{d}", "forest", d, n) for n in [5,10,20,50] for d in [4,6,8]]
NAMES = [c[0] for c in configs]

def make(c):
    _, fam, d, n = c
    if fam == "tree": return DecisionTreeClassifier(max_depth=d, random_state=SEED)
    return RandomForestClassifier(n_estimators=n, max_depth=d, random_state=SEED, n_jobs=1)

def hard(model, Xt):
    v = np.zeros((len(Xt), 3), dtype=int)
    for e in model.estimators_:
        t = e.tree_
        assert list(e.classes_.astype(int)) == [0, 1, 2]
        lc = e.classes_.astype(int)[np.argmax(t.value[:, 0, :], axis=1)]
        v[np.arange(len(Xt)), lc[e.apply(Xt.astype(np.float32))]] += 1
    return v

def evalfit(c, tr, te):
    m = make(c).fit(X[tr], y[tr])
    Xt, yt = X[te], y[te]
    if c[1] == "forest":
        v = hard(m, Xt); p = np.argmax(v, axis=1)
        tie = int(((v == v.max(1, keepdims=True)).sum(1) > 1).sum())
        soft = float((m.predict(Xt).astype(int) == yt).mean())
        nodes = sum(e.tree_.node_count for e in m.estimators_)
    else:
        p = m.predict(Xt).astype(int); tie = 0; soft = float((p == yt).mean()); nodes = m.tree_.node_count
    rc = [float((p[yt == k] == k).mean()) for k in range(3)]
    return float((p == yt).mean()), soft, tie, nodes, rc

def job(tes):
    out = []
    for c in configs:
        rs = [evalfit(c, ~te, te) for te in tes]
        out.append((np.mean([r[0] for r in rs]), np.mean([r[1] for r in rs]), np.mean([r[2] for r in rs]),
                    np.mean([r[3] for r in rs]), np.mean([r[4] for r in rs], axis=0)))
    return out

t0 = time.time()
redraw = []
for i in range(200):
    rr = np.random.default_rng(SEED + 5000 + i)
    ids = set()
    for c in range(3): ids |= {int(j) for j in rr.choice(all_ids[c], 10, replace=False)}
    te = np.isin(rec, list(ids)); assert te.sum() == 1200
    redraw.append(te)
cvs = []
for r in range(40):
    rr = np.random.default_rng(SEED + 6000 + r)
    f = [set() for _ in range(5)]
    for c in range(3):
        perm = rr.permutation(all_ids[c])
        for k in range(5): f[k] |= {int(j) for j in perm[k*10:(k+1)*10]}
    cvs.append([np.isin(rec, list(s)) for s in f])

A = Parallel(n_jobs=-1)(delayed(job)([te]) for te in redraw)
B = Parallel(n_jobs=-1)(delayed(job)(tes) for tes in cvs)
full_nodes = {}
for c in configs:
    m = make(c).fit(X, y)
    full_nodes[c[0]] = int(m.tree_.node_count if c[1] == "tree" else sum(e.tree_.node_count for e in m.estimators_))

def arr(R, idx): return np.array([[r[ci][idx] for ci in range(len(configs))] for r in R])
a_acc, a_soft, a_tie, a_nodes = (arr(A, i) for i in range(4))
a_rc = np.array([[A[i][ci][4] for ci in range(len(configs))] for i in range(200)])
b_acc, b_nodes = arr(B, 0), arr(B, 3)
ref = NAMES.index("dt_d3")
rows = []
for ci, nm in enumerate(NAMES):
    a, b = a_acc[:, ci], b_acc[:, ci]
    da, db = a - a_acc[:, ref], b - b_acc[:, ref]
    rows.append(dict(
        name=nm, full_nodes=full_nodes[nm], full_bytes=10*full_nodes[nm],
        mean_nodes=float((a_nodes[:, ci].sum()*1 + b_nodes[:, ci].sum()*1) / 240), mean_bytes=float(10*(a_nodes[:, ci].sum() + b_nodes[:, ci].sum())/240),
        a_mean=a.mean(), a_sd=a.std(ddof=1), a_min=a.min(), a_p5=float(np.percentile(a, 5)), a_ge90=int((a >= THRESH).sum()),
        b_mean=b.mean(), b_sd=b.std(ddof=1), b_min=b.min(), b_ge90=int((b >= THRESH).sum()),
        a_diff=da.mean(), a_diff_se=da.std(ddof=1)/np.sqrt(200), b_diff=db.mean(), b_diff_se=db.std(ddof=1)/np.sqrt(40),
        a_win=float((a >= a_acc[:, ref]).mean()), b_win=float((b >= b_acc[:, ref]).mean()),
        rec_normal=a_rc[:, ci, 0].mean(), rec_abnormal=a_rc[:, ci, 1].mean(), rec_faulty=a_rc[:, ci, 2].mean(),
        a_tie_mean=a_tie[:, ci].mean() if nm.startswith("rf") else None, a_soft_mean=a_soft[:, ci].mean() if nm.startswith("rf") else None))
res = pd.DataFrame(rows)
res["fp_ok"] = res.mean_bytes <= 100*1024
res["c_adiff"] = res.a_diff >= 0.010; res["c_bdiff"] = res.b_diff >= 0.010
res["c_bwin"] = res.b_win >= 0.75
p5ref = res.loc[res.name == "dt_d3", "a_p5"].iloc[0]
res["c_p5"] = res.a_p5 >= p5ref
res["qualifies"] = res.fp_ok & res.c_adiff & res.c_bdiff & res.c_bwin & res.c_p5
res.loc[res.name == "dt_d3", "qualifies"] = False
res = res.sort_values("a_mean", ascending=False)
res.to_csv(OUTD / "results.csv", index=False)
q = res[res.qualifies].sort_values("mean_bytes")
decision = q.name.iloc[0] if len(q) else "dt_d3"
runtime = time.time() - t0
json.dump(dict(decision=decision, qualifying=list(q.name), runtime_s=runtime, dt_d3_p5=p5ref,
               results=json.loads(res.to_json(orient="records"))), open(OUTD / "results.json", "w"), indent=1)
pd.set_option("display.width", 250, "display.max_columns", 50)
print(res.round(4).to_string(index=False))
print("decision:", decision, "runtime s:", round(runtime))
