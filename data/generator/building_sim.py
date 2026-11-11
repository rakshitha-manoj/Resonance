"""10-DOF shear-building simulator producing MPU6050-like int16 floor-acceleration windows.

Importable; nothing runs on import. `python data/generator/building_sim.py` runs the sanity checks.

Model summary
- Uniform floor mass, uniform storey stiffness before damage. Storey k (1..10) is the column stiffness
  between floor k-1 and floor k (floor 0 = ground). Damage = fractional stiffness loss in one storey.
- Driven by base acceleration (band-limited random). Output = ABSOLUTE floor acceleration (what an
  accelerometer reads), obtained by modal superposition in the frequency domain at 1 kHz.
- 1 kHz -> causal 2nd-order Butterworth at 44 Hz (CFG=3 accel emulation, design()/lfilter_ss() copied from
  models/training/dlpf_check.py) -> every 10th sample (100 Hz) -> sensor noise + offset -> round/clip int16.
- az = lateral (sway) acceleration. gx is MODELLED, NOT PHYSICAL (as in generate.py): gain * lateral signal
  + noise + constant offset.
"""
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.linalg import eigh

FS = 100
N = 128
FS_SIM = 1000
DEC = FS_SIM // FS
NFLOOR = 10
COUNTS_PER_G = 16384
AZ_BASELINE = 16384
DLPF_FC_ACCEL = 44.0  # variant "A" accel cut-off (CFG=3 emulation, ~42 Hz class) in dlpf_check.py
BURN = 6400  # samples at 1 kHz discarded (6.4 s)
BANDS = {"default": (2.0, 12.0), "broadband": (2.0, 45.0)}
LEVEL_RANGE = (0.6, 1.7)  # excitation-level multiplier, log-uniform per building
REF_F1, REF_ZETA, REF_TOP_STD = 7.0, 0.02, 173.0  # reference building / target top-floor az_std (counts)
BATCH = 64


# --------------------------------------------------------------------------------------------------
# copied verbatim from models/training/dlpf_check.py (that file executes on import, so it is not imported)
def design(order, fc, fs):
    k = np.tan(np.pi * np.asarray(fc, dtype=np.float64) / fs)
    if order == 2:
        n = 1 / (1 + np.sqrt(2) * k + k ** 2)
        b = np.stack([k ** 2 * n, 2 * k ** 2 * n, k ** 2 * n])
        a = np.stack([2 * (k ** 2 - 1) * n, (1 - np.sqrt(2) * k + k ** 2) * n])
    else:
        b = np.stack([k / (1 + k), k / (1 + k), np.zeros_like(k)])
        a = np.stack([(k - 1) / (1 + k), np.zeros_like(k)])
    return b, a


def lfilter_ss(x, b, a, step):
    b0, b1, b2 = b
    a1, a2 = a
    x0 = x[:, 0]
    y0 = x0 * (b0 + b1 + b2) / (1 + a1 + a2)
    z2 = b2 * x0 - a2 * y0
    z1 = b1 * x0 - a1 * y0 + z2
    xt = np.ascontiguousarray(x.T)
    out = np.empty((x.shape[0], x.shape[1] // step))
    for i in range(xt.shape[0]):
        xi = xt[i]
        yi = b0 * xi + z1
        z1 = b1 * xi - a1 * yi + z2
        z2 = b2 * xi - a2 * yi
        if i % step == 0:
            out[:, i // step] = yi
        if i == 0:
            assert np.allclose(yi, y0, rtol=1e-9, atol=1e-6)
    return out
# --------------------------------------------------------------------------------------------------


def dlpf_coeffs():
    return design(2, DLPF_FC_ACCEL, FS_SIM)


def dlpf_gain(f):
    b, a = dlpf_coeffs()
    z = np.exp(-1j * 2 * np.pi * np.asarray(f, dtype=np.float64) / FS_SIM)
    return np.abs((b[0] + b[1] * z + b[2] * z ** 2) / (1 + a[0] * z + a[1] * z ** 2))


# ------------------------------------------------------------------ building definition
def building_params(seed):
    """All per-building randomness, from the seed alone."""
    r = np.random.default_rng([0xB1D, int(seed)])
    f1 = r.uniform(4, 10)
    zeta = r.uniform(0.01, 0.03)
    level = float(np.exp(r.uniform(np.log(LEVEL_RANGE[0]), np.log(LEVEL_RANGE[1]))))
    return {
        "seed": int(seed), "f1": float(f1), "zeta": float(zeta), "level": level,
        "az_sigma": r.uniform(20, 60, NFLOOR), "az_off": r.normal(0, 40, NFLOOR),
        "gx_gain": r.uniform(0.02, 0.2, NFLOOR), "gx_sigma": r.uniform(5, 20, NFLOOR), "gx_off": r.normal(0, 15, NFLOOR),
    }


def k0_for_f1(f1):
    """Uniform shear building (M=1): w1 = 2 sqrt(k) sin(pi / (2(2n+1)))."""
    return (2 * np.pi * f1) ** 2 / (4 * np.sin(np.pi / (2 * (2 * NFLOOR + 1))) ** 2)


def storey_stiffness(p, damage=None):
    k = np.full(NFLOOR, k0_for_f1(p["f1"]))
    if damage is not None:
        s, d = damage
        assert 1 <= s <= NFLOOR and 0 <= d < 1
        k[s - 1] *= 1 - d
    return k


def stiffness_matrix(k):
    """Storey s joins floor s-1 (ground if s=1) and floor s; DOF index = floor-1."""
    K = np.zeros((NFLOOR, NFLOOR))
    for s in range(1, NFLOOR + 1):
        i = s - 1
        K[i, i] += k[i]
        if s >= 2:
            K[i - 1, i - 1] += k[i]
            K[i - 1, i] -= k[i]
            K[i, i - 1] -= k[i]
    return K


def modal(p, damage=None):
    lam, V = eigh(stiffness_matrix(storey_stiffness(p, damage)))  # M = I, unit-norm modes
    omega = np.sqrt(lam)
    gamma = V.sum(axis=0)  # participation factors for a uniform base acceleration
    return omega, V * gamma  # coef[floor, mode]; each row sums to 1


def f1_hz(p, damage=None):
    return float(modal(p, damage)[0][0] / (2 * np.pi))


def floor_transfer(p, damage, f):
    """Base acceleration -> absolute floor acceleration, shape (10, len(f)), complex."""
    omega, coef = modal(p, damage)
    w = 2 * np.pi * np.asarray(f, dtype=np.float64)[None, :]
    on = omega[:, None]
    z = p["zeta"]
    T = (on ** 2 + 2j * z * on * w) / (on ** 2 - w ** 2 + 2j * z * on * w)
    return coef @ T


@lru_cache(maxsize=None)
def reference_psd():
    """One-sided base-acceleration PSD S0 (g^2/Hz) so the REFERENCE building (f1=7 Hz, zeta=2%, level 1)
    has a top-floor signal-only az_std of REF_TOP_STD counts. Set once, analytically, from the target
    amplitude; independent of any detection result."""
    lo, hi = BANDS["default"]
    f = np.linspace(lo, hi, 4001)
    p = {"f1": REF_F1, "zeta": REF_ZETA}
    h2 = np.abs(floor_transfer(p, None, f)[NFLOOR - 1] * dlpf_gain(f)) ** 2
    var_per_s0 = np.trapezoid(h2, f)
    return float((REF_TOP_STD / COUNTS_PER_G) ** 2 / var_per_s0)


def excitation_reasoning():
    s0 = reference_psd()
    lo, hi = BANDS["default"]
    return {
        "S0_g2_per_Hz": s0,
        "base_rms_mg_in_2_12Hz": 1000 * float(np.sqrt(s0 * (hi - lo))),
        "reference_building": {"f1_hz": REF_F1, "zeta": REF_ZETA, "target_top_floor_az_std_counts": REF_TOP_STD},
        "level_multiplier": f"log-uniform {LEVEL_RANGE} on base-acceleration amplitude, per building",
        "text": (
            "Base-equivalent ambient excitation (wind/traffic/occupant activity) of about "
            f"{1000 * np.sqrt(s0 * (hi - lo)):.1f} mg rms in 2-12 Hz (flat one-sided PSD S0={s0:.2e} g^2/Hz) is amplified by the "
            "first-mode resonance (quality factor 1/(2*zeta), 17-50) to an expected roof signal-only az_std of ~173 counts "
            "(~10.5 mg rms) for the reference building (f1=7 Hz, zeta=2%). 173 counts is the geometric middle of the required "
            "100-300 count range and sits inside the generator's 'normal' class amplitude (150-500 counts peak = 106-354 rms). "
            "S0 is computed analytically from that amplitude target (reference_psd()) and was fixed before any detection "
            "result existed; per-building level multiplier 0.6-1.7 plus the f1 and zeta spread give the remaining variation. "
            "The same S0 is used for the broadband (2-45 Hz) variant, so broadband has more total power."),
    }


# ------------------------------------------------------------------ simulation
def simulate_batch(params_list, exc_id, n_windows, damages=None, excitation="default", amp_mult=1.0):
    """Signal-only absolute floor acceleration at 100 Hz, counts: (B, 10, n_windows*128).
    Each building's randomness depends only on (its seed, exc_id), so results do not depend on batch composition."""
    lo, hi = BANDS[excitation]
    n100 = n_windows * N
    L = BURN + n100 * DEC
    freqs = np.fft.rfftfreq(L, 1 / FS_SIM)
    mask = (freqs >= lo) & (freqs <= hi)
    fb = freqs[mask]
    amp = np.sqrt(L * reference_psd() * FS_SIM / 4)
    B = len(params_list)
    damages = damages if damages is not None else [None] * B
    x = np.empty((B * NFLOOR, L))
    for i, (p, dmg) in enumerate(zip(params_list, damages)):
        r = np.random.default_rng([0xE, p["seed"], int(exc_id)])
        g = r.standard_normal((2, fb.size))
        X = np.zeros((NFLOOR, freqs.size), dtype=complex)
        X[:, mask] = floor_transfer(p, dmg, fb) * (amp * p["level"] * amp_mult * (g[0] + 1j * g[1]))
        x[i * NFLOOR:(i + 1) * NFLOOR] = np.fft.irfft(X, n=L) * COUNTS_PER_G
    b, a = dlpf_coeffs()
    out = lfilter_ss(x, b, a, DEC)
    return out.reshape(B, NFLOOR, -1)[:, :, BURN // DEC:]


def simulate_many(params_list, exc_id, n_windows, damages=None, excitation="default", amp_mult=1.0, batch=BATCH):
    damages = damages if damages is not None else [None] * len(params_list)
    parts = [simulate_batch(params_list[i:i + batch], exc_id, n_windows, damages[i:i + batch], excitation, amp_mult)
             for i in range(0, len(params_list), batch)]
    return np.concatenate(parts, axis=0)


def expected_floor_std(p, damage=None, excitation="default", amp_mult=1.0):
    """Analytic expectation of signal-only az_std per floor (infinite averaging), counts."""
    lo, hi = BANDS[excitation]
    f = np.linspace(lo, hi, 20001)
    h2 = np.abs(floor_transfer(p, damage, f) * dlpf_gain(f)[None, :]) ** 2
    var = reference_psd() * np.trapezoid(h2, f, axis=1)
    return COUNTS_PER_G * p["level"] * amp_mult * np.sqrt(var)


IMPACT_ENVELOPE_CUTOFF = 50.0  # counts: an impact "matters" in a window while its envelope amp*exp(-t/tau) is >= this
# (about the median per-node sensor noise sigma, 40 counts; the ambient az_std on the top floor is ~170 counts)


def impact_rate_range(f0):
    """Impact rate range (per second) as in generate.py:41: U(0.3, 2.0) when f0 is in the 14-30 Hz (frequency-shift)
    range, U(0.8, 2.0) otherwise."""
    return (0.3, 2.0) if 14.0 <= f0 <= 30.0 else (0.8, 2.0)


def impact_window_mask(T, starts, amps, tau, cutoff=IMPACT_ENVELOPE_CUTOFF):
    """Window-level truth: bool (T // N,), True for every window that contains an impact start or any sample of a
    decay tail whose envelope amp*exp(-t/tau) is still >= cutoff (t in seconds from the impact start)."""
    mask = np.zeros(T // N, dtype=bool)
    for s, a in zip(starts, amps):
        last = min(int(s) + int(np.floor(tau * FS * np.log(a / cutoff))), T - 1)
        mask[int(s) // N:last // N + 1] = True
    return mask


def impact_spikes(T, rng, return_truth=False):
    """generate.py 'faulty' damped-sinusoid impact signature (counts at 100 Hz). Resonant frequency f0 is drawn
    from the generator's two ranges (4-12 Hz, 14-30 Hz) with equal probability (assumption); the rate follows
    impact_rate_range(f0). With return_truth=True returns (spikes, truth) where truth has f0, rate, tau, starts, amps
    and mask (impact_window_mask)."""
    f0 = rng.uniform(14, 30) if rng.random() < 0.5 else rng.uniform(4, 12)
    rate = rng.uniform(*impact_rate_range(f0))
    n_sp = rng.poisson(rate * T / FS)
    tau = rng.uniform(0.08, 0.25)
    spikes = np.zeros(T)
    starts = rng.integers(0, T, n_sp)
    amps = np.empty(n_sp)
    for j, s in enumerate(starts):
        tt = np.arange(T - s) / FS
        sign = rng.choice([-1, 1])
        amps[j] = rng.uniform(1500, 4500)
        spikes[s:] += sign * amps[j] * np.exp(-tt / tau) * np.sin(2 * np.pi * f0 * tt + rng.uniform(0, 2 * np.pi))
    if not return_truth:
        return spikes
    return spikes, {"f0": float(f0), "rate": float(rate), "tau": float(tau), "starts": starts, "amps": amps,
                    "mask": impact_window_mask(T, starts, amps, tau)}


def freq_shift_signal(T, rng):
    """generate.py 'faulty' frequency-shift signature as a LOCAL signal: a sinusoid at f0 ~ U(14, 30) Hz with amplitude
    ~ U(150, 600) counts (the generator's faulty-branch amplitude) and a random phase, present in every window.
    Returns (signal (T,), truth) with truth = f0, amp, mask (all windows True)."""
    f0, amp, ph = rng.uniform(14, 30), rng.uniform(150, 600), rng.uniform(0, 2 * np.pi)
    sig = amp * np.sin(2 * np.pi * f0 * np.arange(T) / FS + ph)
    return sig, {"f0": float(f0), "amp": float(amp), "mask": np.ones(T // N, dtype=bool)}


def add_impact(az_f, gx_f, floor, gx_gain, spikes):
    """Add impact spikes (or any local signal, e.g. freq_shift_signal) to ONE floor (1-based) of float az/gx arrays (10, T), in place."""
    az_f[floor - 1] += spikes
    gx_f[floor - 1] += gx_gain[floor - 1] * spikes


def sensor_output(p, floor_sig, exc_id, impact_floor=None, shift_floor=None, return_truth=False):
    """floor_sig (10, T) signal-only counts (zeros => noise-only recording). Returns az, gx int16 (10, T).
    impact_floor / shift_floor (1-based, optional): add the impact / frequency-shift signature to that floor (random
    draws for the shift come after those of the impact). return_truth=True also returns
    {"impact_mask": bool (T//N,) or None, "shift_mask": ... or None} (windows that contain the signature)."""
    r = np.random.default_rng([0x5E, p["seed"], int(exc_id)])
    shp = floor_sig.shape
    az = AZ_BASELINE + p["az_off"][:, None] + floor_sig + r.standard_normal(shp) * p["az_sigma"][:, None]
    gx = p["gx_off"][:, None] + p["gx_gain"][:, None] * floor_sig + r.standard_normal(shp) * p["gx_sigma"][:, None]
    truth = {"impact_mask": None, "shift_mask": None}
    if impact_floor is not None:
        spikes, tr = impact_spikes(shp[1], r, return_truth=True)
        add_impact(az, gx, impact_floor, p["gx_gain"], spikes)
        truth["impact_mask"] = tr["mask"]
    if shift_floor is not None:
        sh, tr = freq_shift_signal(shp[1], r)
        add_impact(az, gx, shift_floor, p["gx_gain"], sh)
        truth["shift_mask"] = tr["mask"]
    q = lambda v: np.clip(np.rint(v), -32768, 32767).astype(np.int16)
    return (q(az), q(gx), truth) if return_truth else (q(az), q(gx))


def to_windows(x):
    """(..., n*128) -> (..., n, 128) contiguous non-overlapping windows."""
    return x.reshape(*x.shape[:-1], x.shape[-1] // N, N)


def simulate_building(seed, exc_id=1, n_windows=30, damage=None, excitation="default", amp_mult=1.0, impact_floor=None):
    p = building_params(seed)
    sig = simulate_batch([p], exc_id, n_windows, [damage], excitation, amp_mult)[0]
    az, gx = sensor_output(p, sig, exc_id, impact_floor)
    return {"params": p, "signal": sig, "az": az, "gx": gx}


# ------------------------------------------------------------------ sanity checks
def sanity_checks(verbose=True):
    from scipy.signal import welch
    res = {}
    # 1. f1 matches target (eigen-solver vs randomised target), and the spectral peak of a simulation matches too
    errs = []
    for s in range(300):
        p = building_params(s)
        errs.append(abs(f1_hz(p) - p["f1"]) / p["f1"])
        assert 4 <= p["f1"] <= 10 and 0.01 <= p["zeta"] <= 0.03
    assert max(errs) < 1e-9, max(errs)
    peaks = []
    for s in range(6):
        p = building_params(1000 + s)
        sig = simulate_batch([p], 1, 300)[0, NFLOOR - 1]
        f, P = welch(sig, FS, nperseg=4096)
        peaks.append((p["f1"], f[np.argmax(P)]))
        assert abs(peaks[-1][1] - p["f1"]) < 0.2, peaks[-1]
    p = building_params(1000)
    sig = simulate_batch([p], 1, 300, [(1, 0.3)])[0, NFLOOR - 1]
    f, P = welch(sig, FS, nperseg=4096)
    assert abs(f[np.argmax(P)] - f1_hz(p, (1, 0.3))) < 0.2
    res["f1_eig_max_rel_err"] = max(errs)
    res["f1_spectral_peaks_target_vs_measured"] = [(round(a, 3), round(b, 3)) for a, b in peaks]
    # 2. damage lowers f1, more damage lowers it more
    for s in range(20):
        p = building_params(s)
        for st in range(1, 11):
            a, b, c = f1_hz(p), f1_hz(p, (st, 0.15)), f1_hz(p, (st, 0.30))
            assert a > b > c, (s, st, a, b, c)
    res["damage_lowers_f1"] = True
    # 3. determinism, batch independence
    a = simulate_building(5, 1, 4, (3, 0.3))
    b = simulate_building(5, 1, 4, (3, 0.3))
    assert all(np.array_equal(a[k], b[k]) for k in ("signal", "az", "gx"))
    c = simulate_building(5, 2, 4, (3, 0.3))
    assert not np.array_equal(a["az"], c["az"])
    pb = [building_params(s) for s in (5, 6, 7)]
    sb = simulate_batch(pb, 1, 4, [(3, 0.3), None, None])
    assert np.array_equal(sb[0], a["signal"])
    ai = simulate_building(5, 1, 4, None, impact_floor=4)
    bi = simulate_building(5, 1, 4, None, impact_floor=4)
    assert np.array_equal(ai["az"], bi["az"])
    res["deterministic_and_batch_independent"] = True
    # 4. DLPF unity gain at low frequency, via the copied filter on real sine inputs
    t = np.arange(10000) / FS_SIM
    b_, a_ = dlpf_coeffs()
    gains = {}
    for fr in (2.0, 5.0, 10.0, 44.0):
        y = lfilter_ss(np.sin(2 * np.pi * fr * t)[None, :], b_, a_, 1)[0, 2000:]
        gains[fr] = float(np.sqrt(2) * y.std())
    assert all(abs(gains[fr] - 1) < 0.02 for fr in (2.0, 5.0, 10.0)), gains
    assert abs(gains[44.0] - 1 / np.sqrt(2)) < 0.03, gains
    assert all(abs(dlpf_gain(fr) - gains[fr]) < 0.01 for fr in gains)
    res["dlpf_gain_measured"] = gains
    # 5. top-floor signal-only az_std versus the 100-300 count requirement, and analytic vs simulated
    ps = [building_params(2000 + s) for s in range(200)]
    sig = simulate_many(ps, 1, 30)
    top = to_windows(sig[:, NFLOOR - 1]).std(axis=-1)  # per-window
    top = np.sqrt((top ** 2).mean(axis=1))
    exp = np.array([expected_floor_std(p)[NFLOOR - 1] for p in ps])
    assert 100 <= np.median(top) <= 300, np.median(top)
    assert 0.85 < np.mean(top / exp) < 1.15, np.mean(top / exp)
    res["top_floor_az_std_signal_only"] = {"p10": float(np.percentile(top, 10)), "median": float(np.median(top)), "p90": float(np.percentile(top, 90)),
                                          "frac_in_100_300": float(np.mean((top >= 100) & (top <= 300))), "sim_over_analytic_mean": float(np.mean(top / exp))}
    # 6. sensor output ranges
    o = simulate_building(9, 1, 30)
    assert o["az"].dtype == np.int16 and o["az"].shape == (10, 3840) and o["gx"].shape == (10, 3840)
    if verbose:
        print("building_sim sanity checks PASSED")
        for k, v in res.items():
            print(f"  {k}: {v}")
    return res


if __name__ == "__main__":
    sys.exit(0 if sanity_checks() else 1)
