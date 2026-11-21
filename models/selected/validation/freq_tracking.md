# Phase A: natural-frequency tracking -> DAMAGE channel

Simulation only (`building_sim`: uniform 10-storey shear building, stationary band-limited base excitation, uniform-stiffness environmental drift as the only environment effect, no mass change). Everything runs the firmware C code (`f1track.h`, `damage.h`) through the ctypes bridge. Pre-registered rule: see the docstring of `models/training/freq_tracking_eval.py`.

- seeds: {'dev': {'start': 6500000, 'n': 120}, 'test': {'start': 6600000, 'n': 120}}; disjoint from every earlier block and from the reserved ranges (asserted).
- healthy record per building: 2624 windows = 1.02 h (one window per 1.4 s superframe; level 0.5-1.5x per 128-window segment); damage recordings: the same 192 commissioning windows then 200 damaged windows; 120 buildings per block.
- environment: uniform stiffness drift A sin(2 pi t/1800 s + phi), A in (0.0, 0.005, 0.01) (primary 0.5%); damage cells: 25 at A = 0.5%, 9 at the other A (compute budget).

## Selection (development block, A = 0.5%, primary pair)

Rule: maximise mean detection over the 25 cells subject to false DAMAGE <= 1.0/24 h. Top of the 300-config grid (eligible first):

| M | C | delta % | P | false/24 h | false begins | det mean | det 5% | det 10% | det 15% | det 20% | det 30% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 32 **(chosen)** | 64 | 2.00 | 32 | 0.81 | 4 | 0.219 | 0.01 | 0.03 | 0.11 | 0.36 | 0.60 |
| 32 | 128 | 2.00 | 32 | 0.21 | 1 | 0.215 | 0.00 | 0.01 | 0.11 | 0.36 | 0.59 |
| 16 | 128 | 2.50 | 16 | 0.41 | 2 | 0.208 | 0.01 | 0.03 | 0.08 | 0.33 | 0.59 |
| 32 | 64 | 2.50 | 16 | 0.61 | 3 | 0.186 | 0.00 | 0.02 | 0.07 | 0.27 | 0.57 |
| 16 | 64 | 2.00 | 32 | 0.40 | 2 | 0.183 | 0.00 | 0.01 | 0.07 | 0.27 | 0.57 |
| 32 | 128 | 2.50 | 16 | 0.21 | 1 | 0.183 | 0.00 | 0.01 | 0.06 | 0.26 | 0.58 |
| 16 | 128 | 2.00 | 32 | 0.00 | 0 | 0.182 | 0.00 | 0.01 | 0.06 | 0.27 | 0.57 |
| 16 | 32 | 2.00 | 32 | 0.60 | 3 | 0.178 | 0.01 | 0.02 | 0.08 | 0.25 | 0.54 |
| 32 | 128 | 3.00 | 4 | 0.21 | 1 | 0.163 | 0.00 | 0.01 | 0.04 | 0.21 | 0.55 |
| 32 | 128 | 3.00 | 8 | 0.21 | 1 | 0.143 | 0.00 | 0.01 | 0.02 | 0.16 | 0.53 |
| 32 | 64 | 2.50 | 32 | 0.00 | 0 | 0.142 | 0.00 | 0.01 | 0.02 | 0.16 | 0.52 |
| 32 | 32 | 3.00 | 8 | 0.40 | 2 | 0.141 | 0.00 | 0.01 | 0.03 | 0.16 | 0.51 |
| 32 | 64 | 3.00 | 8 | 0.41 | 2 | 0.141 | 0.00 | 0.01 | 0.03 | 0.16 | 0.51 |
| 32 | 128 | 2.50 | 32 | 0.00 | 0 | 0.139 | 0.00 | 0.00 | 0.01 | 0.15 | 0.53 |
| 32 | 32 | 2.50 | 32 | 0.00 | 0 | 0.137 | 0.00 | 0.01 | 0.02 | 0.16 | 0.50 |
| 16 | 64 | 3.00 | 16 | 0.20 | 1 | 0.136 | 0.01 | 0.01 | 0.03 | 0.15 | 0.48 |
| 16 | 32 | 3.00 | 16 | 0.20 | 1 | 0.135 | 0.01 | 0.01 | 0.04 | 0.14 | 0.48 |
| 16 | 128 | 3.00 | 16 | 0.21 | 1 | 0.134 | 0.00 | 0.01 | 0.02 | 0.14 | 0.50 |
| 32 | 128 | 3.00 | 16 | 0.00 | 0 | 0.118 | 0.00 | 0.00 | 0.01 | 0.10 | 0.48 |
| 32 | 64 | 3.00 | 16 | 0.00 | 0 | 0.118 | 0.00 | 0.00 | 0.01 | 0.10 | 0.48 |

**Chosen: M = 32, C = 64, delta = 2.00%, P = 32** (budget met; dev false 0.81/24 h over 118 h, mean detection 0.219).

## TEST block, scored once (95% bootstrap intervals by building)

### False DAMAGE per 24 h of healthy monitoring

| A_env | primary pair (5/10) | hours | secondary pair (3/7) | hours |
|---|---|---|---|---|
| 0% | 0.20 [0.00, 0.61] (1 begins) | 118 | 0.20 [0.00, 0.61] (1) | 118 |
| 0.5% | 0.41 [0.00, 1.22] (2 begins) | 118 | 0.81 [0.00, 2.24] (4) | 118 |
| 1% | 2.24 [0.81, 4.07] (11 begins) | 118 | 2.03 [0.61, 3.86] (10) | 118 |

### Detection rate, primary pair, A = 0.5% (storey x loss; latency in superframes of 1.4 s, detected only)

| storey | true f1 drop % at 5/10/15/20/30% loss | det 5% | det 10% | det 15% | det 20% | det 30% | latency 30% |
|---|---|---|---|---|---|---|---|
| 1 | 0.50/1.04/1.64/2.31/3.88 | 0.03 | 0.07 | 0.33 | 0.84 | 1.00 | 48.2 [46.9, 49.4] |
| 3 | 0.43/0.91/1.43/2.01/3.38 | 0.01 | 0.04 | 0.16 | 0.72 | 1.00 | 52.4 [49.9, 55.3] |
| 5 | 0.31/0.64/1.01/1.43/2.42 | 0.01 | 0.02 | 0.04 | 0.23 | 0.91 | 81.6 [74.6, 88.8] |
| 8 | 0.09/0.20/0.32/0.45/0.77 | 0.00 | 0.00 | 0.01 | 0.01 | 0.03 | 149.0 [85.0, 198.0] |
| 10 | 0.01/0.02/0.04/0.05/0.09 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | n/a |

Mean detection over the 25 cells: **0.217 [0.204, 0.232]**. By loss: 5%: 0.01 [0.00, 0.02], 10%: 0.03 [0.01, 0.04], 15%: 0.11 [0.08, 0.14], 20%: 0.36 [0.33, 0.39], 30%: 0.59 [0.57, 0.60]. By storey: s1: 0.45 [0.42, 0.48], s3: 0.39 [0.36, 0.41], s5: 0.24 [0.22, 0.27], s8: 0.01 [0.00, 0.02], s10: 0.00 [0.00, 0.00].

### Mean detection over the common 9 cells (s in {1,5,10} x d in {10,20,30}%) per A_env and pair

| A_env | primary | secondary |
|---|---|---|
| 0% | 0.336 [0.319, 0.354] | 0.337 [0.319, 0.355] |
| 0.5% | 0.340 [0.321, 0.358] | 0.339 [0.321, 0.357] |
| 1% | 0.343 [0.325, 0.360] | 0.339 [0.322, 0.357] |

Per-cell tables for the other conditions are in the JSON (`test.A*_{primary,secondary}.cells`).

## (i) f1 estimation error vs the eigen f1, healthy windows, test block

| condition | bias Hz | per-building bias sd | mean abs | median | p95 | p99 | max | within-building sd |
|---|---|---|---|---|---|---|---|---|
| A0_M16 pri | -0.010 | 0.006 | 0.043 | 0.034 | 0.115 | 0.168 | 0.464 | 0.053 |
| A0_M16 sec | -0.018 | 0.053 | 0.059 | 0.040 | 0.135 | 0.200 | 7.878 | 0.082 |
| A0_M32 pri | -0.010 | 0.006 | 0.031 | 0.025 | 0.081 | 0.118 | 0.327 | 0.037 |
| A0_M32 sec | -0.023 | 0.019 | 0.042 | 0.031 | 0.104 | 0.149 | 7.763 | 0.052 |
| A0.5_M16 pri | -0.010 | 0.006 | 0.043 | 0.034 | 0.115 | 0.168 | 0.454 | 0.053 |
| A0.5_M16 sec | -0.018 | 0.053 | 0.059 | 0.039 | 0.135 | 0.200 | 7.867 | 0.082 |
| A0.5_M32 pri | -0.010 | 0.006 | 0.031 | 0.025 | 0.082 | 0.119 | 0.326 | 0.037 |
| A0.5_M32 sec | -0.023 | 0.019 | 0.042 | 0.031 | 0.104 | 0.149 | 7.778 | 0.053 |
| A1_M16 pri | -0.010 | 0.006 | 0.043 | 0.034 | 0.115 | 0.168 | 0.462 | 0.053 |
| A1_M16 sec | -0.018 | 0.054 | 0.059 | 0.039 | 0.135 | 0.201 | 7.864 | 0.082 |
| A1_M32 pri | -0.010 | 0.006 | 0.031 | 0.025 | 0.082 | 0.120 | 0.323 | 0.037 |
| A1_M32 sec | -0.022 | 0.025 | 0.043 | 0.031 | 0.104 | 0.150 | 7.797 | 0.056 |

(`pri` = floors 5/10, `sec` = floors 3/7; `A*` = drift amplitude %, `M*` = averaged windows; truth = eigen f1 of the current stiffness, so the drift is NOT counted as error. Large maxima on the secondary pair: on a low floor of a low-f1 building the first mode is weak and the peak jumps to mode 2 (about 3 f1).)

## (ii) Physics: eigen f1 drop vs storey stiffness loss (no estimator involved)

For a uniform shear building the relative drop is independent of f1 (asserted over 20 buildings). Drop in % of f1:

| storey | 5% | 10% | 15% | 20% | 25% | 30% | 35% | 40% | 45% | 50% |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 0.50 | 1.04 | 1.64 | 2.31 | 3.05 | 3.88 | 4.82 | 5.89 | 7.12 | 8.54 |
| 2 | 0.47 | 0.99 | 1.57 | 2.20 | 2.91 | 3.70 | 4.59 | 5.60 | 6.77 | 8.11 |
| 3 | 0.43 | 0.91 | 1.43 | 2.01 | 2.65 | 3.38 | 4.20 | 5.13 | 6.19 | 7.43 |
| 4 | 0.37 | 0.79 | 1.24 | 1.74 | 2.31 | 2.94 | 3.66 | 4.48 | 5.43 | 6.53 |
| 5 | 0.31 | 0.64 | 1.01 | 1.43 | 1.90 | 2.42 | 3.02 | 3.71 | 4.51 | 5.44 |
| 6 | 0.23 | 0.49 | 0.77 | 1.09 | 1.45 | 1.86 | 2.32 | 2.86 | 3.48 | 4.23 |
| 7 | 0.16 | 0.34 | 0.53 | 0.75 | 1.00 | 1.29 | 1.61 | 1.99 | 2.44 | 2.98 |
| 8 | 0.09 | 0.20 | 0.32 | 0.45 | 0.60 | 0.77 | 0.97 | 1.20 | 1.47 | 1.80 |
| 9 | 0.04 | 0.09 | 0.15 | 0.21 | 0.28 | 0.36 | 0.45 | 0.56 | 0.69 | 0.84 |
| 10 | 0.01 | 0.02 | 0.04 | 0.05 | 0.07 | 0.09 | 0.11 | 0.14 | 0.18 | 0.21 |

Damage near the top barely moves f1 (the first mode has little strain energy in the top storeys): this limits ANY f1-based detector, whatever the estimator.

## (iii) Estimated vs true drop (selected tracker, primary pair, mean over the last 80 damaged windows, test)

| cell | true drop % (A=0.5%) | est mean % | est sd % (over buildings) | est-true rms % |
|---|---|---|---|---|
| s1_d05 | 0.50 | 0.47 | 0.48 | 0.48 |
| s1_d10 | 1.04 | 1.04 | 0.49 | 0.49 |
| s1_d15 | 1.64 | 1.60 | 0.45 | 0.46 |
| s1_d20 | 2.31 | 2.26 | 0.46 | 0.46 |
| s1_d30 | 3.88 | 3.88 | 0.47 | 0.47 |
| s3_d05 | 0.43 | 0.43 | 0.52 | 0.52 |
| s3_d10 | 0.91 | 0.83 | 0.53 | 0.53 |
| s3_d15 | 1.43 | 1.43 | 0.46 | 0.46 |
| s3_d20 | 2.01 | 2.01 | 0.51 | 0.51 |
| s3_d30 | 3.38 | 3.35 | 0.47 | 0.47 |
| s5_d05 | 0.31 | 0.29 | 0.46 | 0.46 |
| s5_d10 | 0.64 | 0.56 | 0.54 | 0.55 |
| s5_d15 | 1.02 | 0.96 | 0.50 | 0.51 |
| s5_d20 | 1.43 | 1.41 | 0.46 | 0.46 |
| s5_d30 | 2.42 | 2.40 | 0.41 | 0.41 |
| s8_d05 | 0.09 | 0.10 | 0.50 | 0.50 |
| s8_d10 | 0.20 | 0.18 | 0.47 | 0.47 |
| s8_d15 | 0.32 | 0.37 | 0.48 | 0.48 |
| s8_d20 | 0.45 | 0.40 | 0.47 | 0.48 |
| s8_d30 | 0.77 | 0.79 | 0.50 | 0.50 |
| s10_d05 | 0.01 | -0.06 | 0.49 | 0.50 |
| s10_d10 | 0.02 | -0.02 | 0.46 | 0.46 |
| s10_d15 | 0.04 | 0.03 | 0.49 | 0.49 |
| s10_d20 | 0.05 | -0.03 | 0.48 | 0.49 |
| s10_d30 | 0.09 | 0.05 | 0.49 | 0.49 |

## (v) Coarser zoom grid (dev buildings, M = 16)

| grid step | bins | cost vs 181 | mean abs err Hz | p95 | max | bias | within-building sd |
|---|---|---|---|---|---|---|---|
| 0.05Hz | 181 | 1.00 | 0.0421 | 0.1113 | 0.2487 | -0.0079 | 0.0490 |
| 0.10Hz | 91 | 0.50 | 0.0421 | 0.1114 | 0.2486 | -0.0079 | 0.0490 |
| 0.15Hz | 61 | 0.34 | 0.0421 | 0.1114 | 0.2484 | -0.0080 | 0.0490 |
| 0.20Hz | 46 | 0.25 | 0.0421 | 0.1114 | 0.2482 | -0.0081 | 0.0490 |
| 0.30Hz | 31 | 0.17 | 0.0422 | 0.1113 | 0.2476 | -0.0084 | 0.0489 |
| 0.50Hz | 19 | 0.10 | 0.0423 | 0.1113 | 0.2457 | -0.0092 | 0.0488 |

## Post-hoc robustness fix (NOT a selection): gated tracker, same M=32 / C=64 / delta=2% / P=32

Found by independent verification AFTER the TEST block had been scored: on a low floor of a low-f1 building the first mode is weak and the peak jumps to mode 2 (about 3 f1); such estimates were counted valid, the median of a two-mode commissioning set was meaningless, and the pooled drop was swamped (a spot check with the hardware default floors 1/2 sat in false DAMAGE 17-79% of the time). The fix (in `f1track.h`, run here by the same C code on the cached RAW estimates, no re-simulation): (a) commissioning only if the last C valid estimates are unimodal ((p90-p10)/median <= 0.10, else keep sliding); (b) after commissioning an estimate outside f1_0 x (1 +- 0.20) is invalid; (c) the master already ignores invalid. The configuration was NOT re-selected. OLD = the ungated rows above (scored once); NEW = gated, on dev and on test (test numbers are therefore not a clean one-shot any more).

| condition | pair | OLD dev false/24 h | NEW dev | OLD test false/24 h | NEW test | OLD test det (cells) | NEW test det |
|---|---|---|---|---|---|---|---|
| A=0% | primary | 0.20 [0.00, 0.61] | 0.20 [0.00, 0.61] | 0.20 [0.00, 0.61] | 0.20 [0.00, 0.61] | 0.336 [0.319, 0.354] (9) | 0.336 [0.319, 0.354] (9) |
| A=0% | secondary | 0.41 [0.00, 1.02] | 0.41 [0.00, 1.02] | 0.20 [0.00, 0.61] | 0.20 [0.00, 0.61] | 0.337 [0.319, 0.355] (9) | 0.341 [0.323, 0.358] (9) |
| A=0.5% | primary | 0.81 [0.00, 1.83] | 0.81 [0.00, 1.83] | 0.41 [0.00, 1.22] | 0.41 [0.00, 1.22] | 0.217 [0.204, 0.232] (25) | 0.217 [0.204, 0.232] (25) |
| A=0.5% | secondary | 1.02 [0.20, 2.03] | 1.02 [0.20, 2.03] | 0.81 [0.00, 2.24] | 0.81 [0.00, 2.24] | 0.217 [0.204, 0.230] (25) | 0.218 [0.205, 0.233] (25) |
| A=1% | primary | 2.44 [0.81, 4.27] | 2.44 [0.81, 4.27] | 2.24 [0.81, 4.07] | 2.24 [0.81, 4.07] | 0.343 [0.325, 0.360] (9) | 0.343 [0.325, 0.360] (9) |
| A=1% | secondary | 2.85 [1.22, 5.08] | 2.85 [1.22, 5.08] | 2.03 [0.61, 3.86] | 2.24 [0.61, 4.27] | 0.339 [0.322, 0.357] (9) | 0.342 [0.325, 0.359] (9) |

NEW test, A = 0.5%, primary pair: by loss 5%: 0.01 [0.00, 0.02], 10%: 0.03 [0.01, 0.04], 15%: 0.11 [0.08, 0.14], 20%: 0.36 [0.33, 0.39], 30%: 0.59 [0.57, 0.60]; by storey s1: 0.45 [0.42, 0.48], s3: 0.39 [0.36, 0.41], s5: 0.24 [0.22, 0.27], s8: 0.01 [0.00, 0.02], s10: 0.00 [0.00, 0.00].
On the cached blocks the gates change little (the cached mode-2 floors mostly commissioned consistently on mode 2). One-building spot checks with the hardware default floors 1/2 (700 healthy windows, M=32, C=64, delta 2%, P=32): seed 7900176 false DAMAGE active 30.7% of the time with the old tracker (2 begins) vs 0% gated; seed 7900015 21.1% (3 begins) vs 0%. Caveat: on floor 1 (and floor 2 of 7900015) of these f1 = 4.0-4.1 Hz buildings the tracker commissions ONTO MODE 2 (about 11.6 Hz) consistently, so it then tracks mode 2, not the first natural frequency; low floors of low-f1 buildings are poor sensors for this method.
Gate statistics (test, A = 0.5%): primary: {'slave_floors': 240, 'not_commissioned_in_segment': 0, 'rejected_healthy_windows': 0, 'valid_healthy_windows': 622320}; secondary: {'slave_floors': 240, 'not_commissioned_in_segment': 0, 'rejected_healthy_windows': 214, 'valid_healthy_windows': 622106}.

## Evaluation design caveats (disclosed, not re-simulated)

- The damage recordings are effectively DRIFT-FREE at every A_env: the damaged segment's mid-time (1940 s) is about 1.003 drift periods (1800 s) after the commissioning mid-time, so eps there equals eps at commissioning to within 2e-4 in stiffness. The stability of the common-9 detection table across A_env is therefore an artifact; A_env only affects the FALSE-alarm side. Real drift between commissioning and a damage event is arbitrary, so detection under drift is NOT tested here. 'A_env 0.5%' means +-0.5% stiffness = +-0.25% in f1 (f1 scales with sqrt(1+eps)).
- The selected configuration sits on the grid edge (M = 32 and P = 32 are both the largest values tried); the runner-up (M=32, C=128, delta 2%, P=32) is practically tied (0.215 vs 0.219 dev mean detection, 1 vs 4 false begins). Dev had 4 false begins in 118 h against a budget of about 4.9, so the selection is noisy and may not carry the budget to other data (test: 2 begins at A = 0.5%, 11 at 1%).
- The example figure cell was switched from (storey 3, 20% loss) to (storey 3, 30% loss) AFTER seeing the test example, because the 20% example showed no onset in 200 windows. Presentation only; nothing was selected on it. The figure caption says so.
- `det_mean` for A = 0 and A = 1% averages only the 9 reduced cells (see `det_mean_n_cells`); compare conditions on the common-9 column, not on `det_mean`.
- The '~15-20 ms' per-window `spec_zoom` figure in the project notes is an UNMEASURED assumption (23k MACs; the verifier estimates about 1 ms on silicon, Wokwi is slower); measure it on hardware before relying on the slave timing budget.


24 dev buildings x floors 5/10 x 12 averages of 16 windows; error vs eigen f1 (includes the per-building bias; the within-building sd is what a relative drop sees)

## Runtimes

This run (cache hits): {'dev_block_s': 0.3082568645477295, 'test_block_s': 0.23553991317749023, 'total': 24.290157556533813}.

Simulation of the two full blocks (120 buildings x 3 A_env x (2624 healthy + 25/9/9 x 200 damaged windows)) ran in parallel with 3 workers each on a shared, heavily loaded 12-core PC: about 12-25 min of wall time per round of 3 chunks of 12 buildings (about 730 s of CPU per chunk, 10 chunks per block); on an idle machine expect roughly 40 min per block with 3 workers. Evaluation (selection of 300 configs, bootstrap, tables) from the cache: about 20 s.

## Limitations (honest)

- Uniform shear building, f1 only; one mode, stationary excitation with a flat band (2-12 Hz); real ambient excitation is not white and has narrowband components (machinery, traffic) that sit on the spectrum and bias a peak-picker.
- The only environment effect is a uniform stiffness drift (temperature-like); no mass change (occupancy, snow, equipment), no humidity/boundary effects, no excitation-amplitude dependence of f1 (real structures soften at higher amplitude).
- Damage is a stiffness loss in one storey at once, in a building with no initial nonuniformity; the commissioned f1_0 is assumed to be taken on an undamaged structure.
- The estimator bias is per building (about +-0.1 Hz); the detector works on the relative drop, so it only sees CHANGES; a slow damage that develops during the 1.02 h healthy window would be absorbed only if it happens before commissioning completes.
- Packet loss is not modelled here (every slave reports every superframe); the detector holds its state with no report, and pools whatever slaves report.
- Slave timing: spec_zoom per-window cost is unmeasured (see the caveats); `f1t_t` is 23.4 KB and must be static/global.

