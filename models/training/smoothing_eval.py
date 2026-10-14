import json
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeClassifier

root = Path(__file__).resolve().parents[2]
spec = json.loads((root / "models/selected/feature_spec.json").read_text())
cols = spec["input_columns"]
df = pd.read_csv(root / "data/processed/dataset.csv", usecols=["recording_id", "label"] + cols)
rec = df["recording_id"].values
y = df["label"].map({"normal": 0, "abnormal": 1, "faulty": 2}).values.astype(int)
X = df[cols].values.astype(np.float64)
ids = list(dict.fromkeys(rec.tolist()))
rows = {r: np.where(rec == r)[0] for r in ids}
assert len(ids) == 150 and len(df) == 6000
assert all(len(v) == 40 and (np.diff(v) == 1).all() for v in rows.values())
rec_class = {r: int(y[rows[r][0]]) for r in ids}
all_ids = {c: np.array(sorted(r for r in ids if rec_class[r] == c)) for c in range(3)}
REPEATS = 10
LEAD = 10
TAIL = 10
SEG = 40
HIST_KEYS = ["0", "1", "2", "3", "4", "5-9", "10-19", "20-39", "miss"]
BUCKETS = [(0, 0, "0"), (1, 1, "1"), (2, 2, "2"), (3, 3, "3"), (4, 4, "4"), (5, 9, "5-9"), (10, 19, "10-19"), (20, 39, "20-39")]


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
    return np.array(out)


def oof_predictions(r):
    rr = np.random.default_rng(42 + 8000 + r)
    fids = [set() for _ in range(5)]
    for c in range(3):
        perm = rr.permutation(all_ids[c])
        for k in range(5):
            fids[k] |= {int(i) for i in perm[k * 10:(k + 1) * 10]}
    oof = np.full(len(df), -1)
    for k in range(5):
        te = np.isin(rec, list(fids[k]))
        m = DecisionTreeClassifier(max_depth=3, random_state=42).fit(X[~te], y[~te])
        oof[te] = m.predict(X[te])
    assert (oof >= 0).all()
    return oof


def episodes(s):
    s = np.asarray(s)
    prev = np.concatenate([[0], s[:-1]])
    return int(((s != 0) & (prev == 0)).sum())


def changes(s):
    s = np.asarray(s)
    return int((s[1:] != s[:-1]).sum())


def first_hit(s, target):
    h = np.where(np.asarray(s) == target)[0]
    return int(h[0]) if len(h) else None


def bucket(v):
    if v is None:
        return "miss"
    return next(k for lo, hi, k in BUCKETS if lo <= v <= hi)


def delay_stats(vals):
    got = [x for x in vals if x is not None]
    h = dict.fromkeys(HIST_KEYS, 0)
    for v in vals:
        h[bucket(v)] += 1
    return {"mean": float(np.mean(got)) if got else float("nan"), "median": float(np.median(got)) if got else float("nan"), "misses": len(vals) - len(got), "hist": h}


def average(items):
    first = items[0]
    if isinstance(first, dict):
        return {k: average([i[k] for i in items]) for k in first}
    return float(np.nanmean(items))


per_repeat = []
err_rate = {r: [] for r in ids}
isolated_frac = []
for rep in range(REPEATS):
    oof = oof_predictions(rep)
    err = oof != y
    iso = 0
    for r in ids:
        err_rate[r].append(float(err[rows[r]].mean()))
        e = err[rows[r]]
        for i in range(40):
            if e[i] and not (i > 0 and e[i - 1]) and not (i < 39 and e[i + 1]):
                iso += 1
    isolated_frac.append(iso / max(int(err.sum()), 1))
    normal_ids = all_ids[0]
    a = {k: {"raw": [], "smoothed": []} for k in ("episodes", "recs_with_alarm", "frac_non_normal", "state_changes", "acc")}
    for r in normal_ids:
        raw = oof[rows[r]]
        for name, s in (("raw", raw), ("smoothed", smooth(raw))):
            a["episodes"][name].append(episodes(s))
            a["recs_with_alarm"][name].append(int(episodes(s) > 0))
            a["frac_non_normal"][name].append(float((s != 0).mean()))
            a["state_changes"][name].append(changes(np.concatenate([[0], s])))
            a["acc"][name].append(float((s == 0).mean()))
    d = {k: {"raw": [], "smoothed": []} for k in ("delay_to_true_class", "delay_to_non_normal", "recovery_delay")}
    cnt = {k: {"raw": [], "smoothed": []} for k in ("spurious_changes_per_sequence", "lead_in_false_alarm_episodes")}
    extra = dict.fromkeys(["neg", "0", "1", "2", "3", "4+", "missed_only_smoothed", "missed_only_raw", "missed_both"], 0)
    ok = {"all": {"raw": 0, "smoothed": 0}, "fault": {"raw": 0, "smoothed": 0}, "excl": {"raw": 0, "smoothed": 0}}
    keep = np.ones(LEAD + SEG + TAIL, bool)
    keep[[LEAD, LEAD + SEG]] = False
    faults = [r for c in (1, 2) for r in all_ids[c]]
    for j, r in enumerate(faults):
        c = rec_class[r]
        raw = np.concatenate([oof[rows[normal_ids[j % 50]]][-LEAD:], oof[rows[r]], oof[rows[normal_ids[(j + 25) % 50]]][:TAIL]])
        truth = np.concatenate([np.zeros(LEAD, int), np.full(SEG, c), np.zeros(TAIL, int)])
        hit = {}
        for name, s in (("raw", raw), ("smoothed", smooth(raw))):
            seg = s[LEAD:LEAD + SEG]
            hit[name] = first_hit(seg, c)
            d["delay_to_true_class"][name].append(hit[name])
            d["delay_to_non_normal"][name].append(next(iter(np.where(seg != 0)[0]), None))
            d["recovery_delay"][name].append(first_hit(s[LEAD + SEG:], 0))
            cnt["spurious_changes_per_sequence"][name].append(changes(np.concatenate([[0], s])) - 2)
            cnt["lead_in_false_alarm_episodes"][name].append(episodes(s[:LEAD]))
            ok["all"][name] += int((s == truth).sum())
            ok["fault"][name] += int((seg == c).sum())
            ok["excl"][name] += int((s == truth)[keep].sum())
        if hit["raw"] is None or hit["smoothed"] is None:
            extra["missed_both" if hit["raw"] is None and hit["smoothed"] is None else "missed_only_smoothed" if hit["smoothed"] is None else "missed_only_raw"] += 1
        else:
            x = hit["smoothed"] - hit["raw"]
            extra["neg" if x < 0 else "4+" if x >= 4 else str(x)] += 1
    n_seq = len(faults)
    n_norm = len(normal_ids) * 40
    res = {"a_normal": {}, "b_onset": {}, "b_onset_counts": {}, "b_extra_delay_smoothed_minus_raw": extra, "c_accuracy": {}}
    for k, v in a.items():
        if k != "acc":
            res["a_normal"][k] = {n: float(np.mean(v[n])) if k != "recs_with_alarm" else float(np.sum(v[n])) for n in v}
    for k, v in d.items():
        res["b_onset"][k] = {n: delay_stats([None if x is None else int(x) for x in v[n]]) for n in v}
    for k, v in cnt.items():
        res["b_onset_counts"][k] = {n: float(np.mean(v[n])) for n in v}
    res["c_accuracy"]["all_sequences_a_plus_b"] = {n: (float(np.sum(a["acc"][n])) * 40 + ok["all"][n]) / (n_norm + n_seq * (LEAD + SEG + TAIL)) for n in ("raw", "smoothed")}
    res["c_accuracy"]["all_sequences_excl_first_window_after_each_transition"] = {n: (float(np.sum(a["acc"][n])) * 40 + ok["excl"][n]) / (n_norm + n_seq * int(keep.sum())) for n in ("raw", "smoothed")}
    res["c_accuracy"]["fault_segments_only"] = {n: ok["fault"][n] / (n_seq * SEG) for n in ("raw", "smoothed")}
    res["c_accuracy"]["normal_recordings_only"] = {n: float(np.mean(a["acc"][n])) for n in ("raw", "smoothed")}
    per_repeat.append(res)

summary = average(per_repeat)
er = np.array([np.mean(err_rate[r]) for r in ids])
cls_arr = np.array([rec_class[r] for r in ids])
rec_stats = {
    "consistently_misclassified_mean_error_ge_0.5": int((er >= 0.5).sum()),
    "partially_misclassified_0_lt_mean_error_lt_0.5": int(((er > 0) & (er < 0.5)).sum()),
    "never_misclassified": int((er == 0).sum()),
    "consistently_misclassified_by_class": {str(c): int(((er >= 0.5) & (cls_arr == c)).sum()) for c in range(3)},
    "frac_of_error_windows_isolated_single": float(np.mean(isolated_frac)),
}
result = {
    "rule": "symmetric 2-of-3 over last 3 raw classes; state starts normal; no majority keeps state",
    "protocol": "OOF predictions, grouped stratified 5-fold over 150 recordings (10 rec/class per test fold), 10 repeats seeds 42+8000+r, DecisionTreeClassifier(max_depth=3, random_state=42), 18 features; values averaged over repeats (hist = mean count per repeat; nan = no detections)",
    "onset_sequence": f"last {LEAD} windows of normal recording j%50 + 40 fault windows + first {TAIL} of normal recording (j+25)%50, j over 100 fault recordings; delay in windows from onset (0 = first fault window); recovery = windows after fault end until state normal (0 = first tail window); misses excluded from mean/median; recs_with_alarm is a count of 50 normal recordings; spurious_changes_per_sequence = state changes from an initial normal state minus the 2 required (onset, recovery); extra delay = per-sequence smoothed minus raw delay to true class, counts per 100 sequences; the 2 transitions per 60-window sequence are far denser than in deployment, so the excl-first-window accuracy isolates the 1-window lag",
    "repeats": REPEATS,
    "results": summary,
    "recordings": rec_stats,
}
out_path = root / "models/selected/validation/smoothing_eval.json"
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(result, indent=2))
print("(a) normal recordings, 50 x 40 windows")
for k, v in summary["a_normal"].items():
    print(f"  {k}: raw {v['raw']:.3f} smoothed {v['smoothed']:.3f}")
print("(b) onset, 100 fault recordings x 60 windows")
for k, v in summary["b_onset"].items():
    for name in ("raw", "smoothed"):
        s = v[name]
        print(f"  {k} {name}: mean {s['mean']:.3f} median {s['median']:.2f} misses {s['misses']:.2f} hist {{{', '.join(f'{h}:{c:.1f}' for h, c in s['hist'].items())}}}")
for k, v in summary["b_onset_counts"].items():
    print(f"  {k}: raw {v['raw']:.3f} smoothed {v['smoothed']:.3f}")
print("  extra delay smoothed-raw per 100 sequences", {k: round(v, 1) for k, v in summary["b_extra_delay_smoothed_minus_raw"].items()})
print("(c) accuracy")
for k, v in summary["c_accuracy"].items():
    print(f"  {k}: raw {v['raw']:.4f} smoothed {v['smoothed']:.4f}")
print("recordings", json.dumps(rec_stats))
