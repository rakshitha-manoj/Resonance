import json, time, warnings
from pathlib import Path
import numpy as np, pandas as pd
from joblib import Parallel, delayed
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import LinearSVC, SVC
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

configs = [("dt_d3", "tree", None)]
configs += [(f"svm_lin_c{c}", "lin", c) for c in [0.1, 1, 10]]
configs += [(f"svm_rbf_c{c}", "rbf", c) for c in [1, 10, 100]]
NAMES = [c[0] for c in configs]

def make(c):
    _, fam, p = c
    if fam == "tree": return DecisionTreeClassifier(max_depth=3, random_state=SEED)
    if fam == "lin": return Pipeline([("s", StandardScaler()), ("m", LinearSVC(C=p, dual="auto", max_iter=20000, random_state=SEED))])
    return Pipeline([("s", StandardScaler()), ("m", SVC(kernel="rbf", C=p, gamma="scale", random_state=SEED))])

def nsv(m, fam):
    return int(m.named_steps["m"].n_support_.sum()) if fam == "rbf" else 0

def evalfit(c, tr, te):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        m = make(c).fit(X[tr], y[tr])
    nw = sum("onverge" in str(x.message) for x in w)
    yt = y[te]
    p = m.predict(X[te]).astype(int)
    rc = [float((p[yt == k] == k).mean()) for k in range(3)]
    return float((p == yt).mean()), nsv(m, c[1]), nw, rc

def job(tes):
    out = []
    for c in configs:
        rs = [evalfit(c, ~te, te) for te in tes]
        out.append((np.mean([r[0] for r in rs]), [r[1] for r in rs], sum(r[2] for r in rs), np.mean([r[3] for r in rs], axis=0)))
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

def arr(R, idx): return np.array([[r[ci][idx] for ci in range(len(configs))] for r in R])
a_acc, b_acc = arr(A, 0), arr(B, 0)
a_rc = np.array([[A[i][ci][3] for ci in range(len(configs))] for i in range(200)])
a_warn, b_warn = arr(A, 2), arr(B, 2)
ref = 0
print("dt_d3 reproduction: a_mean", round(a_acc[:, ref].mean(), 5), "b_mean", round(b_acc[:, ref].mean(), 4))

full_nsv = {}
for c in configs:
    if c[1] == "rbf": full_nsv[c[0]] = nsv(make(c).fit(X, y), "rbf")

def rbf_bytes(n): return n * 18 * 4 + n * 2 * 4 + 3 * 4 + 144
LIN_F32 = (3 * 18 + 3) * 4 + 144
LIN_I8 = 3 * 18 + 3 * 4 + 3 * 4 + 144

p5ref = float(np.percentile(a_acc[:, ref], 5))
rows = []
for ci, nm in enumerate(NAMES):
    fam = configs[ci][1]
    a, b = a_acc[:, ci], b_acc[:, ci]
    da, db = a - a_acc[:, ref], b - b_acc[:, ref]
    fits = np.concatenate([np.array([r[ci][1] for r in A]).ravel(), np.concatenate([r[ci][1] for r in B])]) if fam == "rbf" else None
    if fam == "rbf":
        mean_sv, max_sv = float(fits.mean()), int(fits.max())
        mean_b, max_b, full_b = rbf_bytes(mean_sv), rbf_bytes(max_sv), rbf_bytes(full_nsv[nm])
        ops = f"{max_sv}x(18 MAC+1 exp) max"
    elif fam == "lin":
        mean_sv = max_sv = None; mean_b = max_b = full_b = LIN_F32; ops = "54 MAC"
    else:
        mean_sv = max_sv = None; mean_b = max_b = full_b = None; ops = "tree"
    rows.append(dict(name=nm, mean_sv=mean_sv, max_sv=max_sv, full_sv=full_nsv.get(nm),
        mean_bytes=mean_b, max_bytes=max_b, full_bytes=full_b, int8_bytes=LIN_I8 if fam == "lin" else None, ops=ops,
        a_mean=a.mean(), a_sd=a.std(ddof=1), a_min=a.min(), a_p5=float(np.percentile(a, 5)), a_ge90=int((a >= THRESH).sum()),
        b_mean=b.mean(), b_sd=b.std(ddof=1), b_min=b.min(),
        a_diff=da.mean(), a_diff_se=da.std(ddof=1)/np.sqrt(200), b_diff=db.mean(), b_diff_se=db.std(ddof=1)/np.sqrt(40),
        a_win=float((a >= a_acc[:, ref]).mean()), b_win=float((b >= b_acc[:, ref]).mean()),
        rec_normal=a_rc[:, ci, 0].mean(), rec_abnormal=a_rc[:, ci, 1].mean(), rec_faulty=a_rc[:, ci, 2].mean(),
        conv_warnings=int(a_warn[:, ci].sum() + b_warn[:, ci].sum())))
res = pd.DataFrame(rows)
res["fp_ok"] = res.max_bytes <= 100 * 1024
res["c_adiff"] = res.a_diff >= 0.010; res["c_bdiff"] = res.b_diff >= 0.010
res["c_bwin"] = res.b_win >= 0.75; res["c_p5"] = res.a_p5 >= p5ref
res["qualifies"] = res.fp_ok & res.c_adiff & res.c_bdiff & res.c_bwin & res.c_p5
res.loc[res.name == "dt_d3", ["fp_ok", "qualifies"]] = [True, False]
res.to_csv(OUTD / "svm_results.csv", index=False)
q = res[res.qualifies]
runtime = time.time() - t0
json.dump(dict(decision=list(q.name) if len(q) else "dt_d3", runtime_s=runtime, dt_d3_p5=p5ref,
               results=json.loads(res.to_json(orient="records"))), open(OUTD / "svm_results.json", "w"), indent=1)
pd.set_option("display.width", 250, "display.max_columns", 60)
print(res.round(4).T.to_string())
print("qualifying:", list(q.name), "runtime s:", round(runtime))
