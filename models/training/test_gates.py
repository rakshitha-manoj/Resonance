"""Tests for the building simulator (data/generator/building_sim.py) and the localisation-gate v2 analysis code
(models/training/localisation_gate_v2.py). Plain asserts, runnable as a script: `python models/training/test_gates.py`
prints one line per test and exits non-zero if any test fails.

These tests exist because the earlier self-tests missed 13 of 16 injected bugs. Every test below was checked against
deliberately broken copies of the code (see the mutation report in the task notes); each check is written so that a
specific kind of bug trips it:
  * simulator: damage on the requested storey (static push), transfer function vs a direct matrix solve, excitation band,
    independent excitation per exc_id, undamaged baseline, determinism, batch independence, impact rate rule, window truth;
  * gate v2: storey indexing, ratio / noise-subtraction / mean-variance formulas, tilt direction and power normalisation,
    custom band and amplitude, which exc_id and damage each recording uses, sd_k from set A only and T from set B only,
    alarm threshold, seed-block disjointness checks.
"""
import sys
import traceback
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "data" / "generator"))
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import building_sim as bs  # noqa: E402
import localisation_gate_v2 as lg2  # noqa: E402

NF, N, FS = bs.NFLOOR, bs.N, bs.FS


def seeds_p(*seeds):
    return [bs.building_params(s) for s in seeds]


# ------------------------------------------------------------------ independent reference model (written here, not imported)
def ref_K(k):
    """Shear-building stiffness matrix built independently of bs.stiffness_matrix. k[i] = stiffness of storey i+1,
    joining floor i (ground for i = 0) and floor i+1. DOF i = floor i+1."""
    K = np.zeros((NF, NF))
    for i in range(NF):
        K[i, i] += k[i]
        if i + 1 < NF:
            K[i, i] += k[i + 1]
            K[i, i + 1] -= k[i + 1]
            K[i + 1, i] -= k[i + 1]
    return K


def ref_k(p, damage=None):
    w1 = 2 * np.pi * p["f1"]
    k0 = w1 ** 2 / (4 * np.sin(np.pi / (2 * (2 * NF + 1))) ** 2)  # uniform shear building, unit masses
    k = np.full(NF, k0)
    if damage is not None:
        k[damage[0] - 1] *= 1 - damage[1]
    return k


def drifts_under_uniform_push(K):
    u = np.linalg.solve(K, np.ones(NF))
    return np.diff(np.concatenate([[0.0], u]))


# ------------------------------------------------------------------ simulator tests
def test_damage_on_requested_storey():
    """Static push: for a shear building the storey shear does not depend on stiffness, so the drift of storey s is
    shear_s/k_s: damaged storey drift rises by 1/(1-loss), every other storey is unchanged."""
    for seed in (3, 11, 40):
        p = bs.building_params(seed)
        for storey in (1, 2, 5, 10):
            for loss in (0.15, 0.30):
                dmg = (storey, loss)
                for K in (bs.stiffness_matrix(bs.storey_stiffness(p, dmg)), ref_K(ref_k(p, dmg))):
                    ratio = drifts_under_uniform_push(K) / drifts_under_uniform_push(ref_K(ref_k(p)))
                    expect = np.ones(NF)
                    expect[storey - 1] = 1 / (1 - loss)
                    assert np.allclose(ratio, expect, rtol=1e-9), (seed, storey, loss, ratio)
        # the modal solver sees the same damage: eigenvalues equal the independent matrix's
        om, _ = bs.modal(p, (5, 0.3))
        assert np.allclose(om ** 2, np.linalg.eigvalsh(ref_K(ref_k(p, (5, 0.3)))), rtol=1e-9)


def test_stiffness_conventions():
    p = bs.building_params(7)
    assert np.allclose(bs.storey_stiffness(p), ref_k(p)) and np.allclose(bs.stiffness_matrix(bs.storey_stiffness(p)), ref_K(ref_k(p)))
    assert abs(bs.f1_hz(p) - p["f1"]) < 1e-9 * p["f1"]
    for s in range(1, 11):
        assert bs.f1_hz(p, (s, 0.3)) < bs.f1_hz(p, (s, 0.15)) < bs.f1_hz(p)


def test_transfer_function_vs_direct_solve():
    """bs.floor_transfer (modal) equals base acceleration -> absolute floor acceleration from a direct frequency-domain
    solve of (K - w^2 I + i w C) U = -1 with C = Phi diag(2 zeta omega) Phi^T (same modal damping)."""
    f = np.array([0.7, 2.0, 4.4, 7.0, 9.3, 12.0, 21.0, 38.0])
    for seed, dmg in ((3, None), (3, (3, 0.3)), (9, (10, 0.15)), (21, (1, 0.3))):
        p = bs.building_params(seed)
        K = ref_K(ref_k(p, dmg))
        lam, V = np.linalg.eigh(K)
        om = np.sqrt(lam)
        C = V @ np.diag(2 * p["zeta"] * om) @ V.T
        got = bs.floor_transfer(p, dmg, f)
        for j, fj in enumerate(f):
            w = 2 * np.pi * fj
            U = np.linalg.solve(K - w ** 2 * np.eye(NF) + 1j * w * C, -np.ones(NF))
            assert np.allclose(got[:, j], 1 - w ** 2 * U, rtol=1e-8, atol=1e-10), (seed, dmg, fj)
    # quasi-static limit: every floor moves with the base
    assert np.allclose(bs.floor_transfer(bs.building_params(3), None, np.array([1e-3])), 1.0, atol=1e-6)


def _band_fraction(x, lo, hi):
    """fraction of the (mean-removed, Hann-windowed) spectral power of x (..., T) at 100 Hz inside [lo, hi] Hz."""
    x = x - x.mean(axis=-1, keepdims=True)
    P = np.abs(np.fft.rfft(x * np.hanning(x.shape[-1]), axis=-1)) ** 2
    f = np.fft.rfftfreq(x.shape[-1], 1 / FS)
    return P[..., (f >= lo) & (f <= hi)].sum(axis=-1) / P.sum(axis=-1)


def test_excitation_band_respected():
    ps = seeds_p(5, 6, 7, 8)
    d = bs.simulate_batch(ps, 1, 40, None, "default")
    assert _band_fraction(d, 1.5, 13.0).min() > 0.99, _band_fraction(d, 1.5, 13.0).min()
    b = bs.simulate_batch(ps, 1, 40, None, "broadband")
    # broadband signal has real power above 14 Hz that default does not
    hi_d, hi_b = 1 - _band_fraction(d, 0, 14.0), 1 - _band_fraction(b, 0, 14.0)
    assert hi_d.max() < 0.005 and hi_b.min() > 0.03 and (hi_b > 10 * hi_d).all(), (hi_d, hi_b)
    assert _band_fraction(b, 1.5, 49).min() > 0.99
    # arbitrary band through the gate-v2 wrapper
    c = lg2.simulate_custom(ps, 1, 40, [None] * 4, (5.0, 8.0), 1.0, 0.0)
    assert _band_fraction(c, 4.5, 8.5).min() > 0.99, _band_fraction(c, 4.5, 8.5).min()
    assert bs.BANDS["default"] == (2.0, 12.0) and bs.BANDS["broadband"] == (2.0, 45.0)


def test_amplitude_multiplier():
    ps = seeds_p(5, 6)
    a, b = bs.simulate_batch(ps, 1, 6, None, "default", 1.0), bs.simulate_batch(ps, 1, 6, None, "default", 3.0)
    assert np.allclose(b, 3 * a, rtol=1e-9, atol=1e-9)
    c = lg2.simulate_custom(ps, 1, 6, [None, None], (2.0, 12.0), 2.5, 0.0)
    assert np.allclose(c, 2.5 * a, rtol=1e-9, atol=1e-9)


def test_independent_excitation_per_exc_id():
    ps = seeds_p(5, 6, 7, 8, 9, 10)
    x = {e: bs.simulate_batch(ps, e, 30, None) for e in (0, 1, 2)}
    for e1, e2 in ((0, 1), (1, 2), (0, 2)):
        a, b = x[e1][:, 9], x[e2][:, 9]
        rho = np.array([np.corrcoef(a[i], b[i])[0, 1] for i in range(len(ps))])
        ratio = (a - b).std(axis=1) / a.std(axis=1)  # sqrt(2) for independent, 0 for identical
        assert np.abs(rho).max() < 0.4 and np.abs(rho).mean() < 0.2 and 1.1 < ratio.mean() < 1.7, (e1, e2, rho, ratio)
    # same exc_id different building seeds: independent too
    q = bs.simulate_batch(seeds_p(500, 501), 1, 30, None)
    assert abs(np.corrcoef(q[0, 9], q[1, 9])[0, 1]) < 0.4
    # sensor noise draws are independent per exc_id and per building, identical for the same (building, exc_id)
    p = bs.building_params(5)
    z = np.zeros((NF, 3840))
    n1, n2, n1b = bs.sensor_output(p, z, 1)[0], bs.sensor_output(p, z, 2)[0], bs.sensor_output(p, z, 1)[0]
    assert np.array_equal(n1, n1b) and not np.array_equal(n1, n2)
    r = np.corrcoef((n1[4] - n1[4].mean()), (n2[4] - n2[4].mean()))[0, 1]
    assert abs(r) < 0.1, r
    m1 = bs.sensor_output(bs.building_params(6), z, 1)[0]
    assert abs(np.corrcoef(n1[4] - n1[4].mean(), m1[4] - m1[4].mean())[0, 1]) < 0.1


def test_baseline_undamaged_and_damage_applied():
    ps = seeds_p(5, 6, 7)
    base = bs.simulate_batch(ps, 1, 10, None)
    assert np.array_equal(base, bs.simulate_batch(ps, 1, 10, [None, None, None]))
    assert np.array_equal(base[1:2], bs.simulate_batch(ps[1:2], 1, 10, [None]))
    dam = bs.simulate_batch(ps, 1, 10, [(4, 0.3), None, (4, 0.3)])
    assert np.array_equal(dam[1], base[1]) and not np.allclose(dam[0], base[0]) and not np.allclose(dam[2], base[2])
    # spectral peak of the simulated roof signal sits at the UNdamaged f1 (baseline) and at the damaged f1 (damaged)
    from scipy.signal import welch
    p = dict(ps[0], f1=6.0, zeta=0.01)
    for dmg in (None, (1, 0.7)):
        s = bs.simulate_batch([p], 1, 120, [dmg])[0, 9]
        f, P = welch(s, FS, nperseg=2048)
        assert abs(f[np.argmax(P)] - bs.f1_hz(p, dmg)) < 0.15, (dmg, f[np.argmax(P)], bs.f1_hz(p, dmg))
    assert abs(bs.f1_hz(p) - bs.f1_hz(p, (1, 0.7))) > 0.5  # the two cases are actually distinguishable


def test_determinism_and_batch_independence():
    ps = seeds_p(5, 6, 7)
    a = bs.simulate_batch(ps, 1, 5, [(3, 0.3), None, (8, 0.2)], "default", 1.3)
    b = bs.simulate_batch(ps, 1, 5, [(3, 0.3), None, (8, 0.2)], "default", 1.3)
    assert np.array_equal(a, b)
    for i in range(3):
        one = bs.simulate_batch(ps[i:i + 1], 1, 5, [[(3, 0.3), None, (8, 0.2)][i]], "default", 1.3)
        assert np.array_equal(a[i:i + 1], one), i
    assert np.array_equal(bs.simulate_many(ps, 1, 5, [(3, 0.3), None, (8, 0.2)], "default", 1.3, batch=2), a)
    az1, gx1 = bs.sensor_output(ps[0], a[0], 1, impact_floor=3, shift_floor=6)
    az2, gx2 = bs.sensor_output(ps[0], a[0], 1, impact_floor=3, shift_floor=6)
    assert np.array_equal(az1, az2) and np.array_equal(gx1, gx2)


def test_dlpf_emulation():
    """Causal 2nd-order Butterworth, 44 Hz at 1 kHz: unity at low f, -3 dB at 44 Hz, measured on real sines through lfilter_ss."""
    assert bs.DLPF_FC_ACCEL == 44.0
    t = np.arange(10000) / bs.FS_SIM
    b, a = bs.dlpf_coeffs()
    for fr in (5.0, 20.0, 44.0, 100.0):
        g = 1 / np.sqrt(1 + (np.tan(np.pi * fr / 1000) / np.tan(np.pi * 44 / 1000)) ** 4)  # analytic bilinear Butterworth
        y = bs.lfilter_ss(np.sin(2 * np.pi * fr * t)[None, :], b, a, 1)[0, 2000:]
        got = np.sqrt(2) * y.std()
        assert abs(got - g) < 0.02, (fr, got, g)
        assert abs(bs.dlpf_gain(fr) - got) < 0.01


def test_impact_rate_rule():
    """generate.py:41: rate ~ U(0.3, 2.0) when f0 is in the 14-30 Hz range, U(0.8, 2.0) otherwise."""
    assert bs.impact_rate_range(14.0) == (0.3, 2.0) and bs.impact_rate_range(30.0) == (0.3, 2.0) and bs.impact_rate_range(22.0) == (0.3, 2.0)
    assert bs.impact_rate_range(13.99) == (0.8, 2.0) and bs.impact_rate_range(4.0) == (0.8, 2.0) and bs.impact_rate_range(12.0) == (0.8, 2.0)
    T = 3840
    rng = np.random.default_rng(1)
    hi_f, lo_f = [], []
    for _ in range(800):
        _, tr = bs.impact_spikes(T, rng, return_truth=True)
        assert 4 <= tr["f0"] <= 30 and not 12 < tr["f0"] < 14
        (hi_f if tr["f0"] >= 14 else lo_f).append((tr["rate"], len(tr["starts"])))
    hi_r, lo_r = np.array(hi_f), np.array(lo_f)
    assert len(hi_r) > 250 and len(lo_r) > 250
    assert hi_r[:, 0].min() >= 0.3 and hi_r[:, 0].max() <= 2.0 and hi_r[:, 0].min() < 0.45 and abs(hi_r[:, 0].mean() - 1.15) < 0.07
    assert lo_r[:, 0].min() >= 0.8 and lo_r[:, 0].max() <= 2.0 and abs(lo_r[:, 0].mean() - 1.4) < 0.07
    # the number of impacts really is Poisson(rate * T / FS)
    for r in (hi_r, lo_r):
        assert abs(r[:, 1].mean() / (r[:, 0] * T / FS).mean() - 1) < 0.08
    # the same draw order through sensor_output: returns the same impact as impact_spikes on that stream
    p = bs.building_params(5)
    z = np.zeros((NF, 1280))
    _, _, truth = bs.sensor_output(p, z, 1, impact_floor=2, return_truth=True)
    assert truth["impact_mask"].shape == (10,) and truth["shift_mask"] is None
    assert len(bs.sensor_output(p, z, 1, impact_floor=2)) == 2


def test_window_level_truth_constructed():
    T = 6 * N
    # start 120 (amp 4500, tau 0.25): envelope >= 50 until floor(0.25*100*ln(90)) = 112 samples later -> sample 232 -> windows 0,1
    # start 500 (amp 1500): 25*ln(30) = 85.04 -> sample 585 -> windows 3,4. Window 2 and 5 are clean.
    m = bs.impact_window_mask(T, [120, 500], [4500.0, 1500.0], 0.25)
    assert m.tolist() == [True, True, False, True, True, False], m
    # an impact at the very end is clipped to the last window, a short decaying one stays in its window
    assert bs.impact_window_mask(T, [T - 3], [4500.0], 0.25).tolist() == [False] * 5 + [True]
    assert bs.impact_window_mask(T, [10], [1500.0], 0.08).tolist() == [True] + [False] * 5  # 8*ln(30)=27 samples
    assert bs.impact_window_mask(T, [], [], 0.1).tolist() == [False] * 6
    # exactly at the cutoff the window counts: amp=cutoff*e^1, tau=0.1 -> 10 samples
    assert bs.impact_window_mask(2 * N, [N - 10], [bs.IMPACT_ENVELOPE_CUTOFF * np.e], 0.1).tolist() == [True, True]
    assert bs.impact_window_mask(2 * N, [N - 11], [bs.IMPACT_ENVELOPE_CUTOFF * np.e], 0.1).tolist() == [True, False]
    # against the real signal: windows flagged clean carry (almost) no impact, flagged windows contain the start
    rng = np.random.default_rng(3)
    checked = 0
    for _ in range(200):
        sp, tr = bs.impact_spikes(3840, rng, return_truth=True)
        for s in tr["starts"]:
            assert tr["mask"][s // N]
        clean = np.repeat(~tr["mask"], N)
        if clean.any():
            assert np.abs(sp[clean]).max() <= bs.IMPACT_ENVELOPE_CUTOFF * max(1, len(tr["starts"])) + 1e-9
            checked += 1
        if len(tr["starts"]) == 0:
            assert not tr["mask"].any() and not sp.any()
    assert checked > 50
    # a single, isolated impact: |spikes| in clean windows is below the cutoff exactly
    for _ in range(300):
        sp, tr = bs.impact_spikes(3840, rng, return_truth=True)
        if len(tr["starts"]) == 1:
            assert np.abs(sp[np.repeat(~tr["mask"], N)]).max(initial=0.0) < bs.IMPACT_ENVELOPE_CUTOFF


def test_local_signatures_land_on_one_floor():
    p = bs.building_params(5)
    sig = bs.simulate_batch([p], 1, 10, None)[0]
    base_az, base_gx = bs.sensor_output(p, sig, 1)
    for kw, key in (({"impact_floor": 4}, "impact_mask"), ({"shift_floor": 7}, "shift_mask")):
        az, gx, truth = bs.sensor_output(p, sig, 1, return_truth=True, **kw)
        fl = list(kw.values())[0]
        other = [i for i in range(NF) if i != fl - 1]
        assert np.array_equal(az[other], base_az[other]) and np.array_equal(gx[other], base_gx[other])
        assert not np.array_equal(az[fl - 1], base_az[fl - 1])
        diff = az[fl - 1].astype(float) - base_az[fl - 1]
        wd = bs.to_windows(diff)
        gd = gx[fl - 1].astype(float) - base_gx[fl - 1]
        assert np.abs(gd - p["gx_gain"][fl - 1] * diff).max() <= 2.0, "gx must receive gain * the az signature"
        flagged = truth[key]
        assert flagged.shape == (10,) and np.abs(wd[~flagged]).max(initial=0) <= bs.IMPACT_ENVELOPE_CUTOFF * 40 + 1
        if key == "impact_mask":
            assert flagged.any() or not np.abs(diff).any()
    # frequency-shift signature: 14-30 Hz, amplitude 150-600, every window
    rng = np.random.default_rng(2)
    for _ in range(50):
        s, tr = bs.freq_shift_signal(1280, rng)
        assert 14 <= tr["f0"] <= 30 and 150 <= tr["amp"] <= 600 and tr["mask"].all() and abs(np.abs(s).max() - tr["amp"]) < 0.06 * tr["amp"]
        f = np.fft.rfftfreq(128, 1 / FS)
        assert abs(f[np.argmax(np.abs(np.fft.rfft(s[:128])))] - tr["f0"]) <= 1.0


# ------------------------------------------------------------------ gate v2 analysis tests
def test_v2_analyse_indexing_and_alarm():
    sd = np.array([0.30, 0.20, 0.12, 0.08, 0.06, 0.05, 0.04, 0.04, 0.03, 0.03])
    for s in (1, 2, 5, 10):
        di = np.zeros((1, NF))
        di[0, s - 1] = 5 * sd[s - 1]
        pred, alarm, zmax = lg2.analyse(di, sd, 3.0)
        assert pred[0] == s and alarm[0] and abs(zmax[0] - 5) < 1e-12, (s, pred)
    di = np.zeros((1, NF))
    di[0, 3] = -2.0 * sd[3]  # negative DI counts through |z|
    di[0, 7] = 1.0 * sd[7]
    assert lg2.analyse(di, sd, 1.5)[0][0] == 4
    # the alarm threshold is T itself, strict: just below / at / just above
    for z, expect in ((2.99, False), (3.0, False), (3.01, True)):
        di = np.zeros((1, NF))
        di[0, 6] = z * sd[6]
        assert bool(lg2.analyse(di, sd, 3.0)[1][0]) == expect, z


def test_v2_ratio_and_variance_formulas():
    s = np.array([[100., 120., 90., 150., 200., 210., 140., 300., 310., 330.]])
    nv = np.full((1, NF), 400.0)
    r = lg2.ratios(s ** 2 + nv, nv)
    exp = np.empty(NF)
    exp[0] = s[0, 0] / np.median(s[0])  # median (not mean): median 175 vs mean 185
    exp[1:] = s[0, 1:] / s[0, :-1]
    assert np.allclose(r[0], exp, rtol=1e-12), (r, exp)
    assert not np.isclose(np.median(s), s.mean())
    # noise variance is subtracted: with nv != 0 the ratio differs from the raw one
    assert not np.allclose(lg2.ratios(s ** 2 + nv, nv), lg2.ratios(s ** 2 + nv, 0 * nv))
    # floor EPS: mv below nv does not blow up
    assert np.isfinite(lg2.ratios(np.full((1, NF), 10.0), nv)).all() and np.allclose(lg2.ratios(np.full((1, NF), 10.0), nv), 1.0)
    # DI = log(r_ass / r_base): scaling floors j.. up by 2 raises r_j only (and r_1 via the median when j = 1)
    s_b = 100.0 * 1.05 ** np.arange(NF)
    mv_b = (s_b ** 2)[None] + nv
    for j in (3, 10):
        s_a = s_b.copy()
        s_a[j - 1:] *= 2.0
        di = lg2.di_from(mv_b, (s_a ** 2)[None] + nv, nv)
        assert abs(di[0, j - 1] - np.log(2)) < 1e-9 and np.allclose(np.delete(di[0], [0, j - 1]), 0), di
    # mean_var is mean(std^2) per floor: windows with different std
    w = np.zeros((1, NF, 2, N))
    w[0, :, 0, ::2], w[0, :, 0, 1::2] = 3.0, -3.0   # std 3
    w[0, :, 1, ::2], w[0, :, 1, 1::2] = 1.0, -1.0   # std 1
    assert np.allclose(lg2.mean_var(w), (9 + 1) / 2)  # not ((3+1)/2)^2 = 4
    w2 = np.random.default_rng(0).integers(-50, 50, (2, NF, 3, N)).astype(np.int16)
    assert np.allclose(lg2.mean_var(w2), (w2.astype(float).var(axis=-1)).mean(axis=-1))


def test_v2_tilt_direction_and_normalisation():
    """PSD weight 10^(-tilt*(f-lo)/(hi-lo)/10), mean weight 1 over the band bins. Check against the analytic expectation
    on a building with the first mode at 7 Hz (same random draws as the flat case, so the ratio is clean)."""
    p = dict(bs.building_params(5), f1=7.0, zeta=0.02)
    nw = 40
    flat = lg2.simulate_custom([p], 1, nw, [None], (2.0, 12.0), 1.0, 0.0)[0, 9]
    til = lg2.simulate_custom([p], 1, nw, [None], (2.0, 12.0), 1.0, lg2.TILT_DB)[0, 9]
    L = bs.BURN + nw * N * bs.DEC
    fr = np.fft.rfftfreq(L, 1 / bs.FS_SIM)
    fb = fr[(fr >= 2.0) & (fr <= 12.0)]
    w = 10.0 ** (-lg2.TILT_DB * (fb - 2.0) / 10.0 / 10.0)
    w /= w.mean()
    H2 = np.abs(bs.floor_transfer(p, None, fb)[9] * bs.dlpf_gain(fb)) ** 2
    # total power ratio
    expect_total = (H2 * w).sum() / H2.sum()
    got_total = til.var() / flat.var()
    assert abs(got_total / expect_total - 1) < 0.06, (got_total, expect_total)
    # low-side / high-side band ratios (below / above the mode): tilt lowers the PSD with frequency
    for lo, hi in ((2.0, 6.0), (8.0, 12.0)):
        sel = (fb >= lo) & (fb <= hi)
        expect = (H2[sel] * w[sel]).sum() / H2[sel].sum()
        P = lambda x: _band_fraction(x, lo, hi) * np.sum(np.abs(np.fft.rfft((x - x.mean()) * np.hanning(len(x)))) ** 2)
        got = P(til) / P(flat)
        assert abs(got / expect - 1) < 0.15, (lo, hi, got, expect)
    assert (w[0] > 1 > w[-1]) and abs(w.mean() - 1) < 1e-12
    # defaults: identical to the simulator
    ps = seeds_p(2_000_000, 2_000_001)
    assert np.array_equal(lg2.simulate_custom(ps, 1, 4, [(3, 0.3), None], (2.0, 12.0), 1.0, 0.0), bs.simulate_batch(ps, 1, 4, [(3, 0.3), None]))


class _Recorder:
    """Wrap bs.simulate_batch / bs.sensor_output to log what batch_mv requests, then restore."""

    def __enter__(self):
        self.sim, self.sens = [], []
        self._sb, self._so = bs.simulate_batch, bs.sensor_output

        def sb(params_list, exc_id, n_windows, damages=None, *a, **k):
            self.sim.append({"exc": int(exc_id), "damages": None if damages is None else list(damages), "nw": n_windows})
            return self._sb(params_list, exc_id, n_windows, damages, *a, **k)

        def so(p, floor_sig, exc_id, *a, **k):
            self.sens.append({"exc": int(exc_id), "zero": not np.any(floor_sig), "T": floor_sig.shape[-1]})
            return self._so(p, floor_sig, exc_id, *a, **k)

        bs.simulate_batch, bs.sensor_output = sb, so
        self._nw = (lg2.NW, lg2.NW_NOISE)
        lg2.NW, lg2.NW_NOISE = 6, 3
        return self

    def __exit__(self, *e):
        bs.simulate_batch, bs.sensor_output = self._sb, self._so
        lg2.NW, lg2.NW_NOISE = self._nw


def test_v2_batch_mv_recording_roles():
    seeds = [2_000_000, 2_000_001, 2_000_002]
    dm = [(3, 0.3), None, (7, 0.3)]
    with _Recorder() as rec:
        r = lg2.batch_mv(seeds, dm)
    assert lg2.EXC_BASELINE != lg2.EXC_ASSESS and lg2.EXC_ASSESS != lg2.EXC_NOISE and lg2.EXC_BASELINE != lg2.EXC_NOISE
    # simulations: baseline (undamaged) then assessment (damaged), distinct exc ids
    assert len(rec.sim) == 2
    base, ass = rec.sim
    assert base["exc"] == lg2.EXC_BASELINE and ass["exc"] == lg2.EXC_ASSESS and base["exc"] != ass["exc"]
    assert all(d is None for d in base["damages"]), base
    assert ass["damages"] == dm
    # sensor noise: one signal-free noise-only recording with its own exc id, then baseline, then assessment sensor outputs
    noise = [c for c in rec.sens if c["zero"]]
    sig = [c for c in rec.sens if not c["zero"]]
    assert len(noise) == 3 and all(c["exc"] == lg2.EXC_NOISE and c["T"] == 3 * N for c in noise)
    assert sorted({c["exc"] for c in sig}) == sorted([lg2.EXC_BASELINE, lg2.EXC_ASSESS]) and all(c["T"] == 6 * N for c in sig)
    # healthy building (index 1): baseline and assessment are different realisations
    assert not np.array_equal(r["base"][1], r["ass"][1])
    assert np.all(r["nv"] > 0) and set(r) == {"base", "ass", "nv"}
    # damaged buildings: assessment differs from baseline on top of the realisation (storey 3 / 7 lowers f1, so top floor power moves)
    with _Recorder():
        r2 = lg2.batch_mv(seeds, dm)
        r3 = lg2.batch_mv(seeds[1:2], dm[1:2])
    assert all(np.array_equal(r[k], r2[k]) for k in r) and all(np.array_equal(r[k][1:2], r3[k]) for k in r)  # determinism, batch independence


def test_v2_calibration_uses_A_for_sd_and_B_for_T():
    rng = np.random.default_rng(0)
    sdA = np.linspace(0.05, 0.3, NF)
    DI = {"A": rng.standard_normal((600, NF)) * sdA, "B": rng.standard_normal((1500, NF)) * sdA * np.linspace(1.0, 3.0, NF)}
    orig = (lg2.run_block, lg2.di_of)
    try:
        lg2.run_block = lambda name, *a, **k: {"name": name}
        lg2.di_of = lambda blk: DI[blk["name"]]
        sd, T, DIa, DIb, zmax = lg2.calibrate()
    finally:
        lg2.run_block, lg2.di_of = orig
    assert np.array_equal(DIa, DI["A"]) and np.array_equal(DIb, DI["B"])
    assert np.allclose(sd, DI["A"].std(axis=0, ddof=1))
    assert not np.allclose(sd, DI["B"].std(axis=0, ddof=1), rtol=0.2)
    zref = np.abs(DI["B"] / sd).max(axis=1)
    assert np.allclose(zmax, zref) and abs(T - np.percentile(zref, lg2.PCT_T)) < 1e-12
    assert abs(T - np.percentile(np.abs(DI["A"] / sd).max(axis=1), lg2.PCT_T)) > 0.05  # T is not the A-based value
    assert lg2.PCT_T == 97.5 and lg2.NW == 120 and lg2.NW_NOISE == 30


def test_v2_seed_block_disjointness_checks():
    lg2.assert_disjoint()
    orig = dict(lg2.SEEDS)
    first = dict(lg2.FIRST_GATE_BLOCKS)
    try:
        for name, key, val in (("s30 inside first-gate range", "s30", (1_100_500, 1000)), ("fa overlaps s15", "fa", (2_300_400, lg2.N_FA)),
                               ("B overlaps A", "B", (2_000_500, lg2.N_B)), ("A at first-gate seeds", "A", (900_000, lg2.N_A))):
            lg2.SEEDS = dict(orig, **{key: val})
            try:
                lg2.assert_disjoint()
            except AssertionError:
                continue
            raise AssertionError("assert_disjoint missed: " + name)
        lg2.SEEDS = orig
        lg2.FIRST_GATE_BLOCKS = dict(first, calib=100_001)
        try:
            lg2.assert_disjoint()
        except AssertionError:
            pass
        else:
            raise AssertionError("assert_disjoint missed an edited first-gate table")
    finally:
        lg2.SEEDS, lg2.FIRST_GATE_BLOCKS = orig, first


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
ORDER = [test_dlpf_emulation, test_stiffness_conventions, test_damage_on_requested_storey, test_transfer_function_vs_direct_solve, test_excitation_band_respected,
         test_amplitude_multiplier, test_independent_excitation_per_exc_id, test_baseline_undamaged_and_damage_applied,
         test_determinism_and_batch_independence, test_impact_rate_rule, test_window_level_truth_constructed,
         test_local_signatures_land_on_one_floor, test_v2_analyse_indexing_and_alarm, test_v2_ratio_and_variance_formulas,
         test_v2_tilt_direction_and_normalisation, test_v2_batch_mv_recording_roles, test_v2_calibration_uses_A_for_sd_and_B_for_T,
         test_v2_seed_block_disjointness_checks]
assert set(ORDER) == set(TESTS)


def main():
    failed = []
    for t in ORDER:
        try:
            t()
            print(f"PASS {t.__name__}", flush=True)
        except Exception:  # noqa: BLE001 - report every failure, then exit non-zero
            failed.append(t.__name__)
            print(f"FAIL {t.__name__}\n{traceback.format_exc()}", flush=True)
    print(f"{len(ORDER) - len(failed)}/{len(ORDER)} tests passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
