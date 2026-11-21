"""R3 (Phase B): does a real TinyML anomaly detector help the fusion alarm? DEVELOPMENT block only (6,100,000+240).

Same recordings, seeds, exc ids and metric machinery as fusion_eval_r2b.py (imported, not copied): per building a healthy
recording (240 windows, level 0.5-1.5x) and a noise-only recording (60), LOCAL events (dense impacts, one sparse impact,
frequency shift, 30 windows) on each slave floor, BUILDING events 2x/3x/5x; metrics false onsets per hour and event recall.
Rules (per slave, flags e_i smoothed 2-of-3 as in fusion.h; then both = BUILDING, one = LOCAL(i), else AMBIENT; debounce k 1-3):
  b  r_i = az_std_i / baseline_i >= T_b                      (the deployed rule, T_b in {1.5, 2, 3}; T_b = 3 is the selected one)
  e  AE ratio_i >= T                                          (int8 autoencoder, score / median commissioning score)
  m  Mahalanobis ratio_i >= T                                 (same normalised inputs, shrunk covariance of the training block)
  f  b(T_b = 3) OR e(T)  per slave, before smoothing the two flags are smoothed separately and OR-ed
  g  b(T_b = 3) OR m(T)
AE / Mahalanobis flags use the node noise gate (az_std < GATE32 = 92.12 counts => not anomalous, applied before smoothing, as in
tiers.h); the ungated variant is reported as a sensitivity row. Thresholds T of e / m: 1.5 ... 50 (fixed grid below).
Selection (pre-registered, identical to R2b, DEV ONLY): maximise mean(LOCAL event recall, BUILDING event recall) subject to
false onsets <= 1.0 / h; ties: fewer false onsets, then simpler (b < e < m < f < g), smaller k, lower T.
The chosen configuration is written to models/selected/validation/fusion_eval_r3_selected.json; a later `--block test --config
<that file>` run (fresh seeds 6,400,000+, NOT done here) scores exactly that configuration once, without any selection code.

Usage (main venv):  python models/training/fusion_eval_r3.py [--small] [--cache DIR]
Prerequisites: models/tinyml/ae_int8.tflite built into the firmware (tools/gen_ae.py -> include/ae_model.h) and mahalanobis.json.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import fusion_eval as fe  # noqa: E402
import fusion_eval_r2b as r2b  # noqa: E402
import fusion_eval_r3_sim as sim  # noqa: E402
import ae_bridge  # noqa: E402

GATE32 = fe.GATE32
OUT_DIR = fe.OUT_DIR
TINY = fe.ROOT / "models" / "tinyml"
SELECTED_JSON = OUT_DIR / "fusion_eval_r3_selected.json"
BLOCKS3 = {"dev": r2b.BLOCKS2["dev"], "small_dev": r2b.BLOCKS2["small_dev"], "test": (6_400_000, 240)}   # test: reserved, scored in the next phase
T_GRID = (1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 30.0, 50.0)
TB_GRID = (1.5, 2.0, 3.0)
TB_SELECTED = 3.0
FAMILY_ORDER = "bemfg"
FOPH_MAX = r2b.FOPH_MAX
K_VALUES = r2b.K_VALUES
FIDX = {f: i for i, f in enumerate(sim.FLOORS)}


# ------------------------------------------------------------------ scores of every recording (AE / Mahalanobis) from the C code
def node_scores(D, names, maha):
    """For every recording name: AE ratio, Mahalanobis ratio, both (nb, 4, nw) float32, per node = (building, slave floor)."""
    nb = D["comm_b20"].shape[0]
    ae = {nm: np.zeros(D[nm + "_b20"].shape[:3], np.float32) for nm in names}
    mh = {nm: np.zeros(D[nm + "_b20"].shape[:3], np.float32) for nm in names}
    mu, P = np.asarray(maha["mean"], np.float64), np.asarray(maha["precision"], np.float64)
    lens = [D[nm + "_b20"].shape[2] for nm in names]
    for b in range(nb):
        for j in range(len(sim.FLOORS)):
            comm = D["comm_b20"][b, j]
            allw = np.concatenate([D[nm + "_b20"][b, j] for nm in names])
            ratio, z, _ = ae_bridge.eval_node(comm, allw)
            comm_ratio, zc, _ = ae_bridge.eval_node(comm, comm)
            d2 = np.einsum("ij,jk,ik->i", z - mu, P, z - mu)
            d2c = np.einsum("ij,jk,ik->i", zc - mu, P, zc - mu)
            mr = (d2 / max(np.median(d2c), 1e-9)).astype(np.float32)
            o = 0
            for nm, n in zip(names, lens):
                ae[nm][b, j] = ratio[o:o + n]
                mh[nm][b, j] = mr[o:o + n]
                o += n
    return ae, mh


# ------------------------------------------------------------------ views and rules
class View3(r2b.PV):
    """r2b.PV (rule b ratios, tree fields unused) plus per-recording AE / Mahalanobis ratios and the az_std at the two slaves."""

    def __init__(self, R, base, fa, fb, ae, mh):
        super().__init__(R, base, fa, fb)
        sel = [FIDX[fa], FIDX[fb]]
        self.ae = {nm: a[:, sel] for nm, a in ae.items()}
        self.mh = {nm: a[:, sel] for nm, a in mh.items()}
        self.gate = {nm: R[nm]["std"][:, [fa - 1, fb - 1]] < np.float32(GATE32) for nm in R}
        self._f = {}

    def flag(self, src, nm, T, gated=True):
        key = (src, nm, T, gated)
        if key not in self._f:
            if src == "b":
                self._f[key] = self.e(nm, T)
            else:
                s = (self.ae if src == "e" else self.mh)[nm]
                x = s >= np.float32(T)
                if gated:
                    x &= ~self.gate[nm]
                self._f[key] = fe.smooth_seq(x.astype(int)) == 1
        return self._f[key]


def decide_e(e):
    e1, e2 = e[:, 0], e[:, 1]
    return np.where(e1 & e2, 3, np.where(e1, 1, np.where(e2, 2, 0)))


def flags_for(cfg, v, nm):
    fam, T = cfg["rule"], cfg["T"]
    g = cfg.get("gated", True)
    if fam == "b":
        return v.flag("b", nm, T)
    if fam in ("e", "m"):
        return v.flag(fam, nm, T, g)
    other = "e" if fam == "f" else "m"
    return v.flag("b", nm, cfg["T_b"]) | v.flag(other, nm, T, g)


def configs3():
    cfgs = []
    for T in TB_GRID:
        cfgs.append({"rule": "b", "T": T, "T_b": T, "gated": True})
    for fam in "em":
        for T in T_GRID:
            cfgs.append({"rule": fam, "T": T, "T_b": None, "gated": True})
    for fam in "fg":
        for T in T_GRID:
            cfgs.append({"rule": fam, "T": T, "T_b": TB_SELECTED, "gated": True})
    out = []
    for c in cfgs:
        for k in K_VALUES:
            d = dict(c, k=k, use_raw=False)
            d["name"] = (f"b, T={c['T']:g}, k={k}" if c["rule"] == "b" else
                         f"{c['rule']}, T={c['T']:g}" + (f" (b T={c['T_b']:g})" if c["T_b"] else "") + f", k={k}")
            out.append(d)
    return out


def run_cfg(cfg, v):
    outs = {nm: r2b.debounce(decide_e(flags_for(cfg, v, nm)), cfg["k"]) for nm in v.needed()}
    return r2b.stats_from(outs, v)


def row_of(cfg, st):
    r = r2b.point_row(dict(cfg), st)
    r.update(family=cfg["rule"], T_b=cfg["T_b"], gated=cfg.get("gated", True))
    return r


def sel_key(r):
    return (r["det_mean"], -r["false_onsets_per_hour"], -FAMILY_ORDER.index(r["rule"]), -r["k"], -r["T"])


def choose(rows, budget=FOPH_MAX):
    elig = [r for r in rows if r["false_onsets_per_hour"] <= budget]
    if elig:
        return max(elig, key=sel_key), True
    return min(rows, key=lambda r: (r["false_onsets_per_hour"],) + tuple(-x for x in sel_key(r))), False


def evaluate_rows(v, cfgs):
    return [row_of(c, run_cfg(c, v)) for c in cfgs]


# ------------------------------------------------------------------ data
def build_view(D, ae, mh, pair=(5, 10)):
    fake = dict(D)
    for nm in r2b.rec_names2():
        fake[nm + "_cls"] = np.zeros(D[nm + "_std"].shape, np.uint8)
    R, base = r2b.derive(fake)
    v = View3(R, base, pair[0], pair[1], ae, mh)
    v._std = {nm: R[nm]["std"][:, [pair[0] - 1, pair[1] - 1]] for nm in R}
    return v


def load_models():
    maha = json.loads((TINY / "mahalanobis.json").read_text())
    return maha


def get_scores(D, maha):
    names = r2b.rec_names2()
    return node_scores(D, names, maha)


# ------------------------------------------------------------------ report
def per_kind(r):
    return {k: r[f"recall_det_{k}"] for k in ("dense", "sparse", "shift", "x2", "x3", "x5")}


def render_md(R):
    def f3(x):
        return "n/a" if x is None else f"{x:.3f}"
    md = ["# R3 (Phase B): int8 autoencoder vs the std-ratio rule, development block", "",
          f"Block: seeds {R['block']}, {R['n_buildings']} buildings, {R['monitoring_hours']:.1f} h of healthy + noise monitoring (primary pair floors 5/10). "
          "Same recordings as R2b (identical signals). Selection (pre-registered, as R2b, development only): maximise mean(LOCAL, BUILDING event recall) "
          "subject to false onsets <= 1.0/h. The AE and the Mahalanobis baseline were trained on healthy data only (seeds 6,300,000-6,300,399), design chosen on the validation split.", "",
          "## Rule-b reproduction check (must equal R2b dev)", "", "| quantity | R2b dev (fusion_eval.json) | this run |", "|---|---|---|"]
    rc = R["reproduction_b_T3_k1"]
    md += [f"| false onsets/h | {rc['expected']['false_onsets_per_hour']:.2f} | {rc['got']['false_onsets_per_hour']:.2f} |",
           f"| LOCAL recall | {rc['expected']['det_local_recall']:.3f} | {rc['got']['det_local_recall']:.3f} |",
           f"| BUILDING recall | {rc['expected']['det_building_recall']:.3f} | {rc['got']['det_building_recall']:.3f} |",
           f"| identical to R2b row (all 21 fields) | | {rc['identical']} |", ""]
    md += ["## Best per rule family under the 1.0 false onsets/h budget (development)", "",
           "| family | config | false onsets/h | LOCAL | BUILDING | mean | dense | sparse | shift | 2x | 3x | 5x |", "|" + "---|" * 12]
    for fam, r in R["best_per_family"].items():
        mark = " **(selected)**" if r["name"] == R["selected"]["name"] else ""
        md.append(f"| {fam} | {r['name']}{mark}{'' if r['eligible'] else ' (over budget)'} | {r['false_onsets_per_hour']:.2f} | {f3(r['det_local_recall'])} | {f3(r['det_building_recall'])} | "
                  f"{f3(r['det_mean'])} | {f3(r['recall_det_dense'])} | {f3(r['recall_det_sparse'])} | {f3(r['recall_det_shift'])} | {f3(r['recall_det_x2'])} | {f3(r['recall_det_x3'])} | {f3(r['recall_det_x5'])} |")
    md += ["", f"Selected: **{R['selected']['name']}** (budget met: {R['selected']['constraint_met']}).", ""]
    md += ["## Bootstrap 95% intervals (by building, 2000 resamples) of the per-family winners", "", "| family | config | false onsets/h | LOCAL | BUILDING | sparse | shift | 2x |", "|---|---|---|---|---|---|---|---|"]
    for fam, ci in R["best_per_family_ci"].items():
        c = lambda k, nd=3: f"{ci[k]['value']:.{nd}f} [{ci[k]['ci95'][0]:.{nd}f}, {ci[k]['ci95'][1]:.{nd}f}]"  # noqa: E731
        md.append(f"| {fam} | {R['best_per_family'][fam]['name']} | {c('false_onsets_per_hour', 2)} | {c('det_local_recall')} | {c('det_building_recall')} | {c('recall_det_sparse')} | {c('recall_det_shift')} | {c('recall_det_x2')} |")
    md += ["", "## Trade-off: best configuration of each family under other false-onset budgets (development)", "",
           "| budget /h | family | config | false onsets/h | LOCAL | BUILDING | mean |", "|---|---|---|---|---|---|---|"]
    for b, fams in R["tradeoff"].items():
        for fam, r in fams.items():
            md.append(f"| {b} | {fam} | {r['name']}{'' if r['budget_met'] else ' (budget not met)'} | {r['false_onsets_per_hour']:.2f} | {f3(r['det_local_recall'])} | {f3(r['det_building_recall'])} | {f3(r['det_mean'])} |")
    md += ["", "## Sensitivity (secondary pair 3/7, same configurations; and the AE without the noise gate)", "", "| config | pair | false onsets/h | LOCAL | BUILDING | mean |", "|---|---|---|---|---|---|"]
    for r in R["sensitivity"]:
        md.append(f"| {r['name']} | {r['pair']} | {r['false_onsets_per_hour']:.2f} | {f3(r['det_local_recall'])} | {f3(r['det_building_recall'])} | {f3(r['det_mean'])} |")
    md += ["", "## Full development table (top 40 rows by the selection key, all families)", "",
           "| config | false onsets/h | LOCAL | BUILDING | mean | dense | sparse | shift | 2x | 3x | 5x |", "|" + "---|" * 11]
    for r in sorted(R["table"], key=sel_key, reverse=True)[:40]:
        md.append(f"| {r['name']}{'' if r['eligible'] else ' (over budget)'} | {r['false_onsets_per_hour']:.2f} | {f3(r['det_local_recall'])} | {f3(r['det_building_recall'])} | {f3(r['det_mean'])} | "
                  f"{f3(r['recall_det_dense'])} | {f3(r['recall_det_sparse'])} | {f3(r['recall_det_shift'])} | {f3(r['recall_det_x2'])} | {f3(r['recall_det_x3'])} | {f3(r['recall_det_x5'])} |")
    md += ["", f"Verdict: {R['verdict']}", ""]
    return "\n".join(md) + "\n"


def run(block="dev", small=False, cache_dir=None, out_dir=None, write=True, config=None):
    t0 = time.time()
    tag = "small_dev" if small else block
    if block == "test":
        raise SystemExit("--block test is reserved for the next phase: it scores the configuration in --config once. Not implemented to run here.")
    sim_seeds = lambda t: list(range(BLOCKS3[t][0], BLOCKS3[t][0] + BLOCKS3[t][1]))  # noqa: E731
    r2b.assert_seed_blocks_disjoint()
    D = sim.load_block(sim_seeds(tag), tag, cache_dir)
    maha = load_models()
    fe.log(f"== R3 {tag}: computing AE / Mahalanobis scores")
    ae, mh = get_scores(D, maha)
    out_dir = Path(out_dir) if out_dir else OUT_DIR
    v = build_view(D, ae, mh, (5, 10))
    cfgs = configs3()
    rows = evaluate_rows(v, cfgs)
    # reproduction check of rule b against the R2b development table
    ref = None
    fj = OUT_DIR / "fusion_eval.json"
    if fj.exists() and not small:
        ref = next(r for r in json.loads(fj.read_text())["rounds"]["R2b"]["development_table_primary_pair"] if r["name"] == "b, T=3, no tree, k=1")
    mine = next(r for r in rows if r["name"] == "b, T=3, k=1")
    repro = {"expected": ref, "got": mine, "identical": (all(abs(mine[k] - ref[k]) < 1e-9 for k in ref if isinstance(ref[k], float) and k in mine) if ref else None)}
    chosen, met = choose(rows)
    best_fam = {}
    for fam in FAMILY_ORDER:
        best_fam[fam], _ = choose([r for r in rows if r["rule"] == fam])
    tradeoff = {}
    for b in (0.5, 1.0, 2.0, 5.0):
        tradeoff[str(b)] = {}
        for fam in FAMILY_ORDER:
            r, ok = choose([r for r in rows if r["rule"] == fam], b)
            tradeoff[str(b)][fam] = dict(r, budget_met=ok)
    B = 200 if small else r2b.N_BOOT
    W = r2b.boot_weights(v.base.shape[0], B)
    ci = {}
    for fam, r in best_fam.items():
        cfg = next(c for c in cfgs if c["name"] == r["name"])
        st = run_cfg(cfg, v)
        pt, bt = r2b.summarize(st), r2b.summarize(st, W)
        ci[fam] = {k: {"value": float(pt[k]), "ci95": fe.ci(bt[k])} for k in ("false_onsets_per_hour", "det_local_recall", "det_building_recall", "recall_det_sparse", "recall_det_shift", "recall_det_x2", "det_mean")}
    sens = []
    v2 = build_view(D, ae, mh, (3, 7))
    for fam in FAMILY_ORDER:
        r = best_fam[fam]
        cfg = next(c for c in cfgs if c["name"] == r["name"])
        sens.append(dict(row_of(cfg, run_cfg(cfg, v2)), pair="3/7"))
        if fam in "efm" and False:
            pass
    for fam in "emfg":
        r = best_fam[fam]
        cfg = dict(next(c for c in cfgs if c["name"] == r["name"]), gated=False)
        cfg["name"] = r["name"] + " [no noise gate]"
        sens.append(dict(row_of(cfg, run_cfg(cfg, v)), pair="5/10"))
    hours = float(r2b.summarize(run_cfg(cfgs[0], v))["monitoring_hours"])
    b_row = best_fam["b"]
    helps = [fam for fam in "emfg" if best_fam[fam]["eligible"] and best_fam[fam]["det_mean"] > b_row["det_mean"] + 1e-9]
    verdict = ("the best rule is " + chosen["name"] + f" (mean recall {chosen['det_mean']:.3f} vs {b_row['det_mean']:.3f} for the best b at <=1/h); families that beat rule b on dev under the budget: "
               + (", ".join(helps) if helps else "none") + ". Development only; a rule using the AE must still be confirmed on the fresh test block.")
    R = {"round": "R3", "block": list(BLOCKS3[tag]), "small_mode": small, "n_buildings": int(v.base.shape[0]), "monitoring_hours": hours,
         "setup": {"T_grid": T_GRID, "T_b_grid": TB_GRID, "T_b_for_f_g": TB_SELECTED, "k": K_VALUES, "budget_per_hour": FOPH_MAX, "noise_gate_counts": GATE32,
                   "mahalanobis": {k: maha[k] for k in maha if k not in ("mean", "precision")}},
         "reproduction_b_T3_k1": repro, "table": rows, "selected": dict(chosen, constraint_met=met), "best_per_family": best_fam,
         "best_per_family_ci": ci, "tradeoff": tradeoff, "sensitivity": sens, "verdict": verdict,
         "per_kind_selected": per_kind(chosen)}
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        fe.jdump(R, out_dir / "fusion_eval_r3_dev.json")
        (out_dir / "fusion_eval_r3_dev.md").write_text(render_md(R), encoding="utf-8")
        fe.jdump({"selected_on": "dev block %s" % (BLOCKS3[tag],), "config": {k: chosen[k] for k in ("name", "rule", "T", "T_b", "k", "gated")}, "dev_row": chosen}, out_dir / "fusion_eval_r3_selected.json")
    fe.log(f"== R3 done in {time.time() - t0:.0f}s; selected {chosen['name']}")
    return R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", default="dev", choices=["dev", "test"])
    ap.add_argument("--small", action="store_true")
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--config", type=Path, default=None, help="selected-configuration JSON (for --block test, next phase)")
    a = ap.parse_args()
    out = a.out or (sim.SCRATCH / "small_out" if a.small else None)
    run(a.block, a.small, a.cache, out, config=a.config)


if __name__ == "__main__":
    main()
