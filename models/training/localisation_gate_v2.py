"""Localisation gate v2 (second and final attempt). Method, normalisation, threshold rule and criterion are fixed
below BEFORE any result existed. Writes models/selected/validation/localisation_gate_v2.json and prints
GATE V2: PASS / GATE V2: FAIL.

Fixed method
  120 contiguous windows for baseline and assessment (default 2-12 Hz excitation unless a sensitivity case says so).
  Per-floor noise variance from a separate 30-window noise-only recording (estimated, not the true sigma).
  s_k = sqrt(max(mean(az_std_k^2) - noise_var_k, EPS)); r_k = s_k/s_(k-1) (k=2..10), r_1 = s_1/median(s);
  DI_k = log(r_k/r0_k); sd_k = std of DI_k over healthy calibration set A; z_k = DI_k/sd_k;
  predicted storey = argmax |z_k|; T = 97.5th percentile of max|z_k| over healthy calibration set B; alarm if max|z| > T.
Criterion (30% loss, default excitation, fresh seeds): exact >= 0.70 AND within-one >= 0.90 AND alarm >= 0.80 AND
  false alarm <= 0.05.
"""
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "data" / "generator"))
import building_sim as bs  # noqa: E402

T0 = time.time()
OUT = ROOT / "models" / "selected" / "validation" / "localisation_gate_v2.json"
SCRATCH = Path(r"C:\Users\raksh\AppData\Local\Temp\claude\121eb6b4f822bd15\8c5ad4ff-5fff-4065-86ee-8a50f4233411\scratchpad\gate_v2")
CACHE = SCRATCH / "cache"
LOGFILE = SCRATCH / "run.log"

FS, N, NF = 100, 128, 10
EPS = 1.0  # same as the first gate
NW = 120  # windows, baseline and assessment
NW_NOISE = 30  # windows, noise-only recording
EXC_BASELINE, EXC_ASSESS, EXC_NOISE = 0, 1, 2
CHUNK = 32  # buildings per simulation batch (120 windows is ~4x the first gate's memory per building)
PCT_T = 97.5

PASS_EXACT, PASS_WITHIN1, PASS_ALARM, PASS_FA = 0.70, 0.90, 0.80, 0.05

# counts
N_A, N_B = 600, 1500  # calibration sets (minimums 500 / 1000)
N_PER_30, N_PER_15, N_FA = 100, 50, 1500  # verdict set (minimums are exactly these)
N_SENS_FA, N_SENS_PER = 300, 20  # sensitivity (minimums)
N_DIAG_CAL, N_DIAG_PER = 300, 20

# seed blocks: (start, n). Disjoint from each other and from the first gate (everything below FIRST_GATE_MAX).
FIRST_GATE_BLOCKS = {"calib": 100_000, "score30": 200_000, "score15": 300_000, "fa": 400_000, "b_calib": 500_000, "b_score30": 600_000,
                     "b_score15": 700_000, "b_fa": 800_000, "partB": 900_000, "noise": 950_000, "expl1": 1_000_000, "expl2": 1_100_000}
FIRST_GATE_MAX = 1_100_000 + 1300  # last first-gate seed used is expl2+1299
SEEDS = {"A": (2_000_000, N_A), "B": (2_100_000, N_B), "s30": (2_200_000, 10 * N_PER_30), "s15": (2_300_000, 10 * N_PER_15),
         "fa": (2_400_000, N_FA),
         "sens_fa_level_x2": (2_500_000, N_SENS_FA), "sens_fa_band_2_15": (2_600_000, N_SENS_FA), "sens_fa_tilt": (2_700_000, N_SENS_FA),
         "sens_30_level_x2": (2_800_000, 10 * N_SENS_PER), "sens_30_band_2_15": (2_900_000, 10 * N_SENS_PER), "sens_30_tilt": (3_000_000, 10 * N_SENS_PER),
         "diag_cal": (3_100_000, N_DIAG_CAL), "diag_s30": (3_200_000, 10 * N_DIAG_PER)}

TILT_DB = 10.0  # PSD falls linearly (in dB) by this much from the lower to the upper band edge, power-normalised
CONDITIONS = {"default": {"band": (2.0, 12.0), "amp": 1.0, "tilt_db": 0.0},
              "level_x2": {"band": (2.0, 12.0), "amp": 2.0, "tilt_db": 0.0},
              "band_2_15": {"band": (2.0, 15.0), "amp": 1.0, "tilt_db": 0.0},
              "tilt": {"band": (2.0, 12.0), "amp": 1.0, "tilt_db": TILT_DB}}


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    try:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(s + "\n")
    except OSError:
        pass


def seeds_of(name):
    a, n = SEEDS[name]
    return list(range(a, a + n))


def assert_disjoint():
    """All v2 blocks disjoint from each other and from every first-gate block (extents taken from its main())."""
    first = json.loads((ROOT / "models/selected/validation/localisation_gate.json").read_text())["meta"]["seed_blocks"]
    assert {k: int(v) for k, v in first.items()} == FIRST_GATE_BLOCKS, "first-gate seed blocks differ from the hard-coded copy"
    rng = sorted((a, a + n, k) for k, (a, n) in SEEDS.items())
    assert rng[0][0] >= FIRST_GATE_MAX, "v2 seeds overlap the first-gate range"
    assert all(rng[i][1] <= rng[i + 1][0] for i in range(len(rng) - 1)), "v2 seed blocks overlap"
    allseeds = [s for k in SEEDS for s in seeds_of(k)]
    assert len(allseeds) == len(set(allseeds)) and min(allseeds) >= FIRST_GATE_MAX


# ------------------------------------------------------------------ simulation (building_sim is not edited; custom excitation is wrapped here)
def simulate_custom(params_list, exc_id, n_windows, damages, band, amp_mult, tilt_db):
    """Copy of bs.simulate_batch with an arbitrary band and optional PSD tilt. Same random draws, so with
    band=(2,12), amp_mult=1, tilt_db=0 it is bit-identical to bs.simulate_batch (tested)."""
    lo, hi = band
    L = bs.BURN + n_windows * bs.N * bs.DEC
    freqs = np.fft.rfftfreq(L, 1 / bs.FS_SIM)
    mask = (freqs >= lo) & (freqs <= hi)
    fb = freqs[mask]
    w = np.ones(fb.size)
    if tilt_db:
        psd_w = 10.0 ** (-tilt_db * (fb - lo) / (hi - lo) / 10.0)
        psd_w = psd_w / psd_w.mean()  # same total band power as flat
        w = np.sqrt(psd_w)  # amplitude weight
    amp = np.sqrt(L * bs.reference_psd() * bs.FS_SIM / 4)
    B = len(params_list)
    x = np.empty((B * NF, L))
    for i, (p, dmg) in enumerate(zip(params_list, damages)):
        r = np.random.default_rng([0xE, p["seed"], int(exc_id)])
        g = r.standard_normal((2, fb.size))
        X = np.zeros((NF, freqs.size), dtype=complex)
        X[:, mask] = bs.floor_transfer(p, dmg, fb) * (amp * p["level"] * amp_mult * w * (g[0] + 1j * g[1]))
        x[i * NF:(i + 1) * NF] = np.fft.irfft(X, n=L) * bs.COUNTS_PER_G
    b, a = bs.dlpf_coeffs()
    out = bs.lfilter_ss(x, b, a, bs.DEC)
    return out.reshape(B, NF, -1)[:, :, bs.BURN // bs.DEC:]


def sim(ps, exc_id, damages, cond):
    c = CONDITIONS[cond]
    if cond == "default":
        return bs.simulate_batch(ps, exc_id, NW, damages, "default", 1.0)
    return simulate_custom(ps, exc_id, NW, damages, c["band"], c["amp"], c["tilt_db"])


def az_windows(ps, sig, exc_id):
    az = np.empty(sig.shape, dtype=np.int16)
    for i, p in enumerate(ps):
        az[i] = bs.sensor_output(p, sig[i], exc_id)[0]
    return bs.to_windows(az)


def mean_var(w):
    return (w.astype(np.float64).std(axis=-1) ** 2).mean(axis=-1)  # mean(az_std^2) per floor


def ratios(mv, nv):
    s = np.sqrt(np.maximum(mv - nv, EPS))
    r = np.empty_like(s)
    r[:, 0] = s[:, 0] / np.median(s, axis=1)
    r[:, 1:] = s[:, 1:] / s[:, :-1]
    return r


def di_from(mv_base, mv_ass, nv):
    return np.log(ratios(mv_ass, nv) / ratios(mv_base, nv))


def batch_mv(seeds, damages, cond="default", clean=False):
    """Per-building per-floor mean(az_std^2) for baseline, assessment and the noise-only recording (and, for the
    diagnosis only, the same quantities from the signal alone with no sensor noise)."""
    res = {k: [] for k in ("base", "ass", "nv") + (("base_c", "ass_c") if clean else ())}
    for i in range(0, len(seeds), CHUNK):
        ps = [bs.building_params(s) for s in seeds[i:i + CHUNK]]
        dm = damages[i:i + CHUNK]
        res["nv"].append(mean_var(az_windows(ps, np.zeros((len(ps), NF, NW_NOISE * N)), EXC_NOISE)))
        sb = sim(ps, EXC_BASELINE, [None] * len(ps), "default")
        res["base"].append(mean_var(az_windows(ps, sb, EXC_BASELINE)))
        if clean:
            res["base_c"].append(mean_var(bs.to_windows(sb)))
        del sb
        sa = sim(ps, EXC_ASSESS, dm, cond)
        res["ass"].append(mean_var(az_windows(ps, sa, EXC_ASSESS)))
        if clean:
            res["ass_c"].append(mean_var(bs.to_windows(sa)))
        del sa
    return {k: np.concatenate(v) for k, v in res.items()}


def run_block(name, damages=None, cond="default", clean=False):
    """Cached per block so a crash does not lose finished work."""
    seeds = seeds_of(name)
    damages = damages if damages is not None else [None] * len(seeds)
    f = CACHE / f"{name}.npz"
    if f.exists():
        z = np.load(f)
        if np.array_equal(z["seeds"], seeds) and all(k in z for k in (("base_c", "ass_c") if clean else ())):
            log(f"  [{name}] loaded from cache ({len(seeds)} buildings)")
            return {k: z[k] for k in z.files if k != "seeds"}
    t = time.time()
    r = batch_mv(seeds, damages, cond, clean)
    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez(f, seeds=np.array(seeds), **r)
    log(f"  [{name}] {len(seeds)} buildings, cond={cond}, {time.time() - t:.0f}s (total {time.time() - T0:.0f}s)")
    return r


def damage_list(lev, n_per):
    true = np.repeat(np.arange(1, 11), n_per)
    return [(int(s), lev) for s in true], true


# ------------------------------------------------------------------ analysis
def analyse(DI, sd, T):
    z = DI / sd
    az = np.abs(z)
    return np.argmax(az, axis=1) + 1, az.max(axis=1) > T, az.max(axis=1)


def wilson(k, n, zc=1.959964):
    if n == 0:
        return [None, None]
    p = k / n
    d = 1 + zc ** 2 / n
    c = (p + zc ** 2 / (2 * n)) / d
    h = zc * np.sqrt(p * (1 - p) / n + zc ** 2 / (4 * n ** 2)) / d
    return [float(c - h), float(c + h)]


def rate(mask):
    k, n = int(np.sum(mask)), int(len(mask))
    return {"rate": k / n, "k": k, "n": n, "ci95": wilson(k, n)}


def score_damaged(DI, true, sd, T):
    pred, alarm, _ = analyse(DI, sd, T)
    exact, w1 = pred == true, np.abs(pred - true) <= 1
    out = {"n": int(len(true)), "exact": rate(exact), "within1": rate(w1), "alarm": rate(alarm), "alarm_and_exact": rate(alarm & exact),
           "per_storey": {}}
    for s in range(1, 11):
        m = true == s
        out["per_storey"][s] = {"n": int(m.sum()), "exact": float(exact[m].mean()), "within1": float(w1[m].mean()),
                                "alarm": float(alarm[m].mean()), "alarm_and_exact": float((alarm & exact)[m].mean())}
    out["predicted_storey_histogram"] = np.bincount(pred, minlength=11)[1:].tolist()
    return out


def score_healthy(DI, sd, T):
    pred, alarm, _ = analyse(DI, sd, T)
    return {"false_alarm": rate(alarm),
            "pred_storey_histogram_all_healthy": np.bincount(pred, minlength=11)[1:].tolist(),
            "pred_storey_histogram_healthy_alarms": np.bincount(pred[alarm], minlength=11)[1:].tolist(),
            "pred_storey_fraction_all_healthy": (np.bincount(pred, minlength=11)[1:] / len(pred)).tolist()}


def di_of(blk):
    return di_from(blk["base"], blk["ass"], blk["nv"])


# ------------------------------------------------------------------ self tests
def self_tests():
    out = {}
    # 1. synthetic injected case, DI level: a large DI at a known storey is predicted and alarmed, storeys 1,5,10
    rng = np.random.default_rng(5)
    sd = np.array([0.30, 0.20, 0.12, 0.08, 0.06, 0.05, 0.04, 0.04, 0.03, 0.03])  # deliberately uneven
    healthy = rng.standard_normal((4000, NF)) * sd
    T = float(np.percentile(np.abs(healthy / sd).max(axis=1), PCT_T))
    for s in (1, 5, 10):
        di = rng.standard_normal((1, NF)) * sd
        di[0, s - 1] = 12 * sd[s - 1]  # a DI of 12 healthy standard deviations at storey s: far above healthy scatter
        pred, alarm, _ = analyse(di, sd, T)
        assert pred[0] == s and alarm[0], (s, pred, alarm)
    # 2. storey indexing through the real pipeline (mean-var -> s_k -> r_k -> DI -> z): amplitudes scaled from floor j up change only r_j
    nvv = np.full((1, NF), 400.0)
    s_base = 100.0 * 1.05 ** np.arange(NF)
    mv_base = (s_base ** 2)[None] + nvv
    for j in (5, 10):
        s_a = s_base.copy()
        s_a[j - 1:] *= 0.5
        di = di_from(mv_base, (s_a ** 2)[None] + nvv, nvv)
        pred, alarm, _ = analyse(di, np.full(NF, 0.1), 0.5)
        assert pred[0] == j and alarm[0], (j, di)
        assert np.allclose(np.delete(di[0], [0, j - 1]), 0), di  # entry 1 also moves because r_1 uses median(s)
    s_a = s_base.copy()
    s_a[0] *= 0.5  # floor 1 alone changes r_1 (down) and r_2 (up); with a larger sd_2 the storey-1 entry dominates
    di = di_from(mv_base, (s_a ** 2)[None] + nvv, nvv)
    pred, alarm, _ = analyse(di, np.array([0.1, 0.5] + [0.1] * 8), 0.5)
    assert pred[0] == 1 and alarm[0]
    out["injected_DI_storeys_1_5_10_predicted_and_alarmed"] = True
    out["pipeline_indexing_storeys_1_5_10"] = True
    # 3. custom excitation wrapper equals bs.simulate_batch when set to defaults
    ps = [bs.building_params(s) for s in (2_000_000, 2_000_001)]
    a = bs.simulate_batch(ps, 1, 4, [(3, 0.3), None])
    b = simulate_custom(ps, 1, 4, [(3, 0.3), None], (2.0, 12.0), 1.0, 0.0)
    assert np.array_equal(a, b)
    c2 = simulate_custom(ps, 1, 4, [None, None], (2.0, 12.0), 2.0, 0.0)
    d = bs.simulate_batch(ps, 1, 4, [None, None], "default", 2.0)
    assert np.array_equal(c2, d)
    tilted = simulate_custom(ps, 1, 4, [None, None], (2.0, 12.0), 1.0, TILT_DB)
    flat = bs.simulate_batch(ps, 1, 4, [None, None])
    assert abs(tilted.std() / flat.std() - 1) < 0.35  # similar total power (normalised), not identical signal
    out["custom_excitation_matches_bs_when_default"] = True
    # 4. determinism per seed and batch independence (real 120-window pipeline on a tiny subset)
    sd3 = [2_000_000, 2_000_001, 2_000_002]
    dm = [(3, 0.3), None, (7, 0.3)]
    r1 = batch_mv(sd3, dm)
    r2 = batch_mv(sd3, dm)
    assert all(np.array_equal(r1[k], r2[k]) for k in r1)
    r3 = batch_mv(sd3[1:2], dm[1:2])
    assert all(np.array_equal(r1[k][1:2], r3[k]) for k in r1)
    out["deterministic_and_batch_independent_120w"] = True
    return out


# ------------------------------------------------------------------ main experiments
def calibrate():
    A = run_block("A")
    DIa = di_of(A)
    sd = DIa.std(axis=0, ddof=1)
    B = run_block("B")
    DIb = di_of(B)
    zmax_b = np.abs(DIb / sd).max(axis=1)  # sd from A only
    T = float(np.percentile(zmax_b, PCT_T))
    return sd, T, DIa, DIb, zmax_b


def verdict_run(sd, T):
    res = {}
    dm30, t30 = damage_list(0.30, N_PER_30)
    s30 = score_damaged(di_of(run_block("s30", dm30)), t30, sd, T)
    dm15, t15 = damage_list(0.15, N_PER_15)
    s15 = score_damaged(di_of(run_block("s15", dm15)), t15, sd, T)
    fa = score_healthy(di_of(run_block("fa")), sd, T)
    crit = {"exact_ge_0.70": bool(s30["exact"]["rate"] >= PASS_EXACT), "within1_ge_0.90": bool(s30["within1"]["rate"] >= PASS_WITHIN1),
            "alarm_ge_0.80": bool(s30["alarm"]["rate"] >= PASS_ALARM), "false_alarm_le_0.05": bool(fa["false_alarm"]["rate"] <= PASS_FA)}
    res["loss_30"], res["loss_15_report_only"], res["healthy_fresh"] = s30, s15, fa
    res["criterion"] = crit
    res["verdict"] = "PASS" if all(crit.values()) else "FAIL"
    return res


def sensitivity(sd, T):
    out = {"note": "off-verdict; baseline default, only the assessment excitation changes; same sd_k and T as the verdict; disjoint seeds",
           "tilt_description": f"PSD weight 10^(-{TILT_DB}*(f-2)/(12-2)/10), i.e. falls linearly in dB by {TILT_DB} dB from 2 to 12 Hz, renormalised "
                               "to equal total band power (mean PSD weight = 1); implemented by scaling the Fourier amplitude by sqrt(weight) in a copy of the simulator's excitation step",
           "cases": {}}
    dm, true = damage_list(0.30, N_SENS_PER)
    for tag, cond in (("level_x2", "level_x2"), ("band_2_15", "band_2_15"), ("tilt", "tilt")):
        fa = score_healthy(di_of(run_block(f"sens_fa_{tag}", None, cond)), sd, T)
        s = score_damaged(di_of(run_block(f"sens_30_{tag}", dm, cond)), true, sd, T)
        out["cases"][tag] = {"condition": CONDITIONS[cond], "false_alarm": fa["false_alarm"], "healthy_pred_storey_fraction": fa["pred_storey_fraction_all_healthy"],
                             "loss_30": {k: s[k] for k in ("n", "exact", "within1", "alarm", "alarm_and_exact", "per_storey")}}
        log(f"  sens {tag}: FA={fa['false_alarm']['rate']:.3f} exact={s['exact']['rate']:.3f} within1={s['within1']['rate']:.3f} alarm={s['alarm']['rate']:.3f}")
    return out


def diagnosis(sd_A, T_A):
    """Noise-free vs noisy scoring on the SAME simulated signals. Targeted: does sd_k from the wrong noise condition,
    or tiny noise-free sd_k at some storeys, explain the low noise-free accuracy?"""
    cal = run_block("diag_cal", None, "default", clean=True)
    dm, true = damage_list(0.30, N_DIAG_PER)
    tst = run_block("diag_s30", dm, "default", clean=True)
    DIc_noisy, DIc_clean = di_from(cal["base"], cal["ass"], cal["nv"]), di_from(cal["base_c"], cal["ass_c"], np.zeros_like(cal["nv"]))
    DIt_noisy, DIt_clean = di_from(tst["base"], tst["ass"], tst["nv"]), di_from(tst["base_c"], tst["ass_c"], np.zeros_like(tst["nv"]))
    sd_noisy_local, sd_clean = DIc_noisy.std(axis=0, ddof=1), DIc_clean.std(axis=0, ddof=1)
    acc = lambda DI, sd: float((np.argmax(np.abs(DI / sd), axis=1) + 1 == true).mean())
    hist = lambda DI, sd: np.bincount(np.argmax(np.abs(DI / sd), axis=1) + 1, minlength=11)[1:].tolist()
    ones = np.ones(NF)
    combos = {
        "noisy_data__sd_A(verdict method)": (DIt_noisy, sd_A),
        "noisy_data__sd_noisy_local": (DIt_noisy, sd_noisy_local),
        "noisy_data__unnormalised": (DIt_noisy, ones),
        "noisy_data__sd_clean(mismatch)": (DIt_noisy, sd_clean),
        "clean_data__sd_clean(matched)": (DIt_clean, sd_clean),
        "clean_data__sd_A_noisy(mismatch)": (DIt_clean, sd_A),
        "clean_data__unnormalised": (DIt_clean, ones)}
    res = {"n_calibration": N_DIAG_CAL, "n_damaged": int(len(true)), "windows": NW, "damage": 0.30,
           "sd_k_A_noisy": sd_A.tolist(), "sd_k_noisy_local": sd_noisy_local.tolist(), "sd_k_clean": sd_clean.tolist(),
           "ratio_sd_clean_over_sd_A": (sd_clean / sd_A).tolist(),
           "exact_accuracy": {k: acc(*v) for k, v in combos.items()},
           "predicted_storey_histogram": {k: hist(*v) for k, v in combos.items()}}
    # systematic (damage-induced) DI at undamaged storeys, in units of each sd: z-scale of the bias that competes with the true signal
    for nm, DI, sd in (("clean", DIt_clean, sd_clean), ("noisy", DIt_noisy, sd_A)):
        zabs = np.abs(DI / sd)
        off = np.array([[np.median(zabs[true == s, k]) for k in range(NF)] for s in range(1, 11)])  # (damaged storey, storey k)
        res[f"median_abs_z_matrix_{nm}"] = off.tolist()
        res[f"median_abs_z_at_true_storey_{nm}"] = [float(off[s, s]) for s in range(NF)]
        res[f"median_abs_z_largest_other_storey_{nm}"] = [float(np.delete(off[s], s).max()) for s in range(NF)]
    # per-storey accuracy, clean matched vs noisy verdict method
    res["per_storey_exact_clean_matched"] = [float((np.argmax(np.abs(DIt_clean / sd_clean), axis=1)[true == s] + 1 == s).mean()) for s in range(1, 11)]
    res["per_storey_exact_noisy_verdict"] = [float((np.argmax(np.abs(DIt_noisy / sd_A), axis=1)[true == s] + 1 == s).mean()) for s in range(1, 11)]
    res["lowest_sd_clean_storeys"] = (np.argsort(sd_clean)[:3] + 1).tolist()
    return res


# ------------------------------------------------------------------ reporting
def fmt(r):
    return f"{r['rate']:.3f} [{r['ci95'][0]:.3f}, {r['ci95'][1]:.3f}]"


def table(s, keys=("exact", "within1", "alarm", "alarm_and_exact")):
    log("    storey:        " + " ".join(f"{k:>5}" for k in range(1, 11)))
    for key in keys:
        log(f"    {key:14s} " + " ".join(f"{s['per_storey'][k][key]:5.2f}" for k in range(1, 11)))


def summary(R):
    V = R["verdict_block"]
    log("\n================ SUMMARY (GATE V2) ================")
    log(f"sd_k (set A, n={N_A}): " + " ".join(f"{v:.3f}" for v in R["calibration"]["sd_k"]))
    log(f"T = {R['calibration']['T_97.5']:.4f} (97.5th pct of max|z| over set B, n={N_B}); alarm rate on B at T = {R['calibration']['alarm_rate_on_B_at_T']:.4f}")
    for lev, key in (("30% loss (VERDICT)", "loss_30"), ("15% loss (report only)", "loss_15_report_only")):
        s = V[key]
        log(f"\n{lev}, n={s['n']}:")
        log(f"  exact {fmt(s['exact'])}  within-one {fmt(s['within1'])}  alarm {fmt(s['alarm'])}  alarm AND exact {fmt(s['alarm_and_exact'])}")
        table(s)
    f = V["healthy_fresh"]
    log(f"\nHealthy fresh (n={f['false_alarm']['n']}): false alarm {fmt(f['false_alarm'])}")
    log("  predicted storey (all healthy, fraction):   " + " ".join(f"{v:.3f}" for v in f["pred_storey_fraction_all_healthy"]))
    log("  predicted storey (healthy alarms, counts): " + " ".join(f"{v:>4d}" for v in f["pred_storey_histogram_healthy_alarms"]))
    log("\nSensitivity (assessment source differs from baseline), 30% loss:")
    for tag, c in R["sensitivity"]["cases"].items():
        log(f"  {tag:10s} FA {fmt(c['false_alarm'])} (n={c['false_alarm']['n']}) | exact {fmt(c['loss_30']['exact'])} within1 {fmt(c['loss_30']['within1'])} alarm {fmt(c['loss_30']['alarm'])} (n={c['loss_30']['n']})")
    d = R["diagnosis"]
    log("\nDiagnosis (exact accuracy at 30%, 120 windows, same signals):")
    for k, v in d["exact_accuracy"].items():
        log(f"  {k:36s} {v:.3f}")
    log("  sd_k clean / sd_k noisy(A): " + " ".join(f"{v:.2f}" for v in d["ratio_sd_clean_over_sd_A"]))
    log("\nCriteria:")
    c, s30 = V["criterion"], V["loss_30"]
    log(f"  exact >= 0.70      {s30['exact']['rate']:.3f}  {'ok' if c['exact_ge_0.70'] else 'FAIL'}")
    log(f"  within-one >= 0.90 {s30['within1']['rate']:.3f}  {'ok' if c['within1_ge_0.90'] else 'FAIL'}")
    log(f"  alarm >= 0.80      {s30['alarm']['rate']:.3f}  {'ok' if c['alarm_ge_0.80'] else 'FAIL'}")
    log(f"  false alarm <= .05 {f['false_alarm']['rate']:.3f}  {'ok' if c['false_alarm_le_0.05'] else 'FAIL'}")
    log(f"runtime {time.time() - T0:.0f} s; wrote {OUT}")
    log("GATE V2: " + V["verdict"])


def main():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    assert_disjoint()
    log("== seed disjointness asserted ==")
    log("== self tests ==")
    tests = self_tests()
    log("  passed:", tests)
    log("== calibration (A: sd_k, B: threshold) ==")
    sd, T, DIa, DIb, zmax_b = calibrate()
    log(f"  sd_k={np.round(sd, 4).tolist()}  T={T:.4f}")
    calib = {"sd_k": sd.tolist(), "T_97.5": T, "n_A": N_A, "n_B": N_B, "alarm_rate_on_B_at_T": float((zmax_b > T).mean()),
             "sd_k_ratio_max_over_min": float(sd.max() / sd.min())}
    log("== verdict scoring (computed before any off-verdict work) ==")
    V = verdict_run(sd, T)
    log(f"  verdict computed: {V['verdict']} {V['criterion']}")
    partial = {"calibration": calib, "verdict_block": V, "self_tests": tests}
    (SCRATCH / "partial_verdict.json").write_text(json.dumps(partial, indent=1))
    log("== sensitivity (off-verdict) ==")
    S = sensitivity(sd, T)
    log("== diagnosis (off-verdict) ==")
    D = diagnosis(sd, T)
    R = {"meta": {"task": "localisation gate v2 (second and final attempt)", "runtime_s": round(time.time() - T0, 1),
                  "method": __doc__, "windows": NW, "noise_windows": NW_NOISE, "eps": EPS, "chunk": CHUNK,
                  "counts": {"A": N_A, "B": N_B, "per_storey_30": N_PER_30, "per_storey_15": N_PER_15, "fresh_healthy": N_FA,
                             "sens_healthy": N_SENS_FA, "sens_per_storey": N_SENS_PER, "diag_cal": N_DIAG_CAL, "diag_per_storey": N_DIAG_PER},
                  "seed_blocks_v2": {k: {"start": a, "n": n} for k, (a, n) in SEEDS.items()}, "first_gate_seed_blocks": FIRST_GATE_BLOCKS,
                  "first_gate_max_seed": FIRST_GATE_MAX, "seed_disjointness_asserted": True,
                  "sd_from": "set A only", "T_from": "set B only (sd_k from A)"},
         "self_tests": tests, "calibration": calib, "verdict_block": V, "verdict": V["verdict"], "sensitivity": S, "diagnosis": D}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(R, indent=1, default=lambda o: o.tolist() if isinstance(o, np.ndarray) else o.item() if isinstance(o, np.generic) else str(o)))
    summary(R)
    return V["verdict"] == "PASS"


if __name__ == "__main__":
    main()
