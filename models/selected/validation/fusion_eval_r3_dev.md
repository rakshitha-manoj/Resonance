# R3 (Phase B): int8 autoencoder vs the std-ratio rule, development block

Block: seeds [6100000, 240], 240 buildings, 28.0 h of healthy + noise monitoring (primary pair floors 5/10). Same recordings as R2b (identical signals). Selection (pre-registered, as R2b, development only): maximise mean(LOCAL, BUILDING event recall) subject to false onsets <= 1.0/h. The AE and the Mahalanobis baseline were trained on healthy data only (seeds 6,300,000-6,300,399), design chosen on the validation split.

## Rule-b reproduction check (must equal R2b dev)

| quantity | R2b dev (fusion_eval.json) | this run |
|---|---|---|
| false onsets/h | 0.79 | 0.79 |
| LOCAL recall | 0.442 | 0.442 |
| BUILDING recall | 0.801 | 0.801 |
| identical to R2b row (all 21 fields) | | True |

## Best per rule family under the 1.0 false onsets/h budget (development)

| family | config | false onsets/h | LOCAL | BUILDING | mean | dense | sparse | shift | 2x | 3x | 5x |
|---|---|---|---|---|---|---|---|---|---|---|---|
| b | b, T=3, k=1 | 0.79 | 0.442 | 0.801 | 0.622 | 0.979 | 0.054 | 0.294 | 0.417 | 0.988 | 1.000 |
| e | e, T=5, k=1 | 0.43 | 0.692 | 0.382 | 0.537 | 0.998 | 0.212 | 0.867 | 0.021 | 0.258 | 0.867 |
| m | m, T=4, k=3 | 0.96 | 0.618 | 0.737 | 0.678 | 0.994 | 0.013 | 0.848 | 0.304 | 0.908 | 1.000 |
| f | f, T=6 (b T=3), k=1 **(selected)** | 0.89 | 0.658 | 0.803 | 0.730 | 0.998 | 0.204 | 0.771 | 0.421 | 0.988 | 1.000 |
| g | g, T=4 (b T=3), k=3 | 1.00 | 0.617 | 0.779 | 0.698 | 0.994 | 0.010 | 0.848 | 0.358 | 0.979 | 1.000 |

Selected: **f, T=6 (b T=3), k=1** (budget met: True).

## Bootstrap 95% intervals (by building, 2000 resamples) of the per-family winners

| family | config | false onsets/h | LOCAL | BUILDING | sparse | shift | 2x |
|---|---|---|---|---|---|---|---|
| b | b, T=3, k=1 | 0.79 [0.25, 1.54] | 0.442 [0.424, 0.461] | 0.801 [0.779, 0.822] | 0.054 [0.035, 0.075] | 0.294 [0.250, 0.338] | 0.417 [0.354, 0.475] |
| e | e, T=5, k=1 | 0.43 [0.18, 0.71] | 0.692 [0.676, 0.709] | 0.382 [0.354, 0.410] | 0.212 [0.179, 0.250] | 0.867 [0.833, 0.898] | 0.021 [0.004, 0.042] |
| m | m, T=4, k=3 | 0.96 [0.43, 1.64] | 0.618 [0.606, 0.630] | 0.737 [0.714, 0.763] | 0.013 [0.004, 0.023] | 0.848 [0.810, 0.881] | 0.304 [0.246, 0.362] |
| f | f, T=6 (b T=3), k=1 | 0.89 [0.32, 1.68] | 0.658 [0.639, 0.677] | 0.803 [0.781, 0.824] | 0.204 [0.171, 0.242] | 0.771 [0.729, 0.810] | 0.421 [0.358, 0.483] |
| g | g, T=4 (b T=3), k=3 | 1.00 [0.46, 1.68] | 0.617 [0.605, 0.629] | 0.779 [0.757, 0.801] | 0.010 [0.002, 0.021] | 0.848 [0.810, 0.881] | 0.358 [0.296, 0.417] |

## Trade-off: best configuration of each family under other false-onset budgets (development)

| budget /h | family | config | false onsets/h | LOCAL | BUILDING | mean |
|---|---|---|---|---|---|---|
| 0.5 | b | b, T=3, k=3 | 0.11 | 0.403 | 0.700 | 0.551 |
| 0.5 | e | e, T=5, k=1 | 0.43 | 0.692 | 0.382 | 0.537 |
| 0.5 | m | m, T=5, k=3 | 0.32 | 0.579 | 0.661 | 0.620 |
| 0.5 | f | f, T=4 (b T=3), k=3 | 0.21 | 0.643 | 0.710 | 0.676 |
| 0.5 | g | g, T=5 (b T=3), k=3 | 0.43 | 0.579 | 0.742 | 0.660 |
| 1.0 | b | b, T=3, k=1 | 0.79 | 0.442 | 0.801 | 0.622 |
| 1.0 | e | e, T=5, k=1 | 0.43 | 0.692 | 0.382 | 0.537 |
| 1.0 | m | m, T=4, k=3 | 0.96 | 0.618 | 0.737 | 0.678 |
| 1.0 | f | f, T=6 (b T=3), k=1 | 0.89 | 0.658 | 0.803 | 0.730 |
| 1.0 | g | g, T=4 (b T=3), k=3 | 1.00 | 0.617 | 0.779 | 0.698 |
| 2.0 | b | b, T=3, k=1 | 0.79 | 0.442 | 0.801 | 0.622 |
| 2.0 | e | e, T=4, k=2 | 1.32 | 0.722 | 0.461 | 0.591 |
| 2.0 | m | m, T=5, k=1 | 1.79 | 0.662 | 0.774 | 0.718 |
| 2.0 | f | f, T=5 (b T=3), k=1 | 1.21 | 0.697 | 0.804 | 0.750 |
| 2.0 | g | g, T=5 (b T=3), k=2 | 1.71 | 0.651 | 0.840 | 0.745 |
| 5.0 | b | b, T=3, k=1 | 0.79 | 0.442 | 0.801 | 0.622 |
| 5.0 | e | e, T=4, k=1 | 2.04 | 0.730 | 0.496 | 0.613 |
| 5.0 | m | m, T=4, k=1 | 4.71 | 0.703 | 0.836 | 0.770 |
| 5.0 | f | f, T=4 (b T=3), k=1 | 2.82 | 0.733 | 0.810 | 0.771 |
| 5.0 | g | g, T=4 (b T=3), k=1 | 4.96 | 0.703 | 0.881 | 0.792 |

## Sensitivity (secondary pair 3/7, same configurations; and the AE without the noise gate)

| config | pair | false onsets/h | LOCAL | BUILDING | mean |
|---|---|---|---|---|---|
| b, T=3, k=1 | 3/7 | 0.64 | 0.493 | 0.724 | 0.608 |
| e, T=5, k=1 | 3/7 | 0.39 | 0.704 | 0.381 | 0.542 |
| m, T=4, k=3 | 3/7 | 1.04 | 0.627 | 0.728 | 0.677 |
| f, T=6 (b T=3), k=1 | 3/7 | 0.75 | 0.675 | 0.728 | 0.701 |
| g, T=4 (b T=3), k=3 | 3/7 | 1.04 | 0.626 | 0.761 | 0.694 |
| e, T=5, k=1 [no noise gate] | 5/10 | 17.89 | 0.695 | 0.382 | 0.539 |
| m, T=4, k=3 [no noise gate] | 5/10 | 12.29 | 0.620 | 0.737 | 0.679 |
| f, T=6 (b T=3), k=1 [no noise gate] | 5/10 | 17.21 | 0.660 | 0.803 | 0.732 |
| g, T=4 (b T=3), k=3 [no noise gate] | 5/10 | 12.32 | 0.619 | 0.779 | 0.699 |

## Full development table (top 40 rows by the selection key, all families)

| config | false onsets/h | LOCAL | BUILDING | mean | dense | sparse | shift | 2x | 3x | 5x |
|---|---|---|---|---|---|---|---|---|---|---|
| f, T=1.5 (b T=3), k=1 (over budget) | 195.61 | 0.951 | 0.992 | 0.972 | 1.000 | 0.854 | 1.000 | 0.975 | 1.000 | 1.000 |
| e, T=1.5, k=1 (over budget) | 195.61 | 0.951 | 0.985 | 0.968 | 1.000 | 0.854 | 1.000 | 0.954 | 1.000 | 1.000 |
| m, T=1.5, k=1 (over budget) | 170.93 | 0.927 | 1.000 | 0.964 | 0.998 | 0.785 | 0.998 | 1.000 | 1.000 | 1.000 |
| g, T=1.5 (b T=3), k=1 (over budget) | 170.93 | 0.927 | 1.000 | 0.964 | 0.998 | 0.785 | 0.998 | 1.000 | 1.000 | 1.000 |
| f, T=1.5 (b T=3), k=2 (over budget) | 134.21 | 0.911 | 0.976 | 0.944 | 0.996 | 0.740 | 0.998 | 0.929 | 1.000 | 1.000 |
| m, T=1.5, k=2 (over budget) | 123.43 | 0.886 | 1.000 | 0.943 | 0.994 | 0.671 | 0.994 | 1.000 | 1.000 | 1.000 |
| g, T=1.5 (b T=3), k=2 (over budget) | 123.43 | 0.886 | 1.000 | 0.943 | 0.994 | 0.671 | 0.994 | 1.000 | 1.000 | 1.000 |
| e, T=1.5, k=2 (over budget) | 134.21 | 0.911 | 0.964 | 0.938 | 0.996 | 0.740 | 0.998 | 0.892 | 1.000 | 1.000 |
| m, T=2, k=1 (over budget) | 78.07 | 0.851 | 0.997 | 0.924 | 1.000 | 0.558 | 0.994 | 0.992 | 1.000 | 1.000 |
| g, T=2 (b T=3), k=1 (over budget) | 78.07 | 0.851 | 0.997 | 0.924 | 1.000 | 0.558 | 0.994 | 0.992 | 1.000 | 1.000 |
| f, T=2 (b T=3), k=1 (over budget) | 90.32 | 0.881 | 0.935 | 0.908 | 1.000 | 0.644 | 1.000 | 0.804 | 1.000 | 1.000 |
| m, T=2, k=2 (over budget) | 53.43 | 0.808 | 0.992 | 0.900 | 0.998 | 0.431 | 0.994 | 0.975 | 1.000 | 1.000 |
| g, T=2 (b T=3), k=2 (over budget) | 53.43 | 0.808 | 0.992 | 0.900 | 0.998 | 0.431 | 0.994 | 0.975 | 1.000 | 1.000 |
| m, T=1.5, k=3 (over budget) | 69.54 | 0.792 | 0.999 | 0.895 | 0.988 | 0.398 | 0.990 | 0.996 | 1.000 | 1.000 |
| g, T=1.5 (b T=3), k=3 (over budget) | 69.54 | 0.792 | 0.999 | 0.895 | 0.988 | 0.398 | 0.990 | 0.996 | 1.000 | 1.000 |
| e, T=2, k=1 (over budget) | 90.21 | 0.881 | 0.894 | 0.888 | 1.000 | 0.644 | 1.000 | 0.696 | 0.988 | 1.000 |
| f, T=1.5 (b T=3), k=3 (over budget) | 77.68 | 0.826 | 0.944 | 0.885 | 0.992 | 0.490 | 0.996 | 0.833 | 1.000 | 1.000 |
| b, T=1.5, k=1 (over budget) | 78.50 | 0.760 | 1.000 | 0.880 | 0.998 | 0.354 | 0.929 | 1.000 | 1.000 | 1.000 |
| e, T=1.5, k=3 (over budget) | 77.68 | 0.826 | 0.932 | 0.879 | 0.992 | 0.490 | 0.996 | 0.800 | 0.996 | 1.000 |
| f, T=2 (b T=3), k=2 (over budget) | 55.57 | 0.833 | 0.890 | 0.861 | 0.998 | 0.500 | 1.000 | 0.671 | 1.000 | 1.000 |
| b, T=1.5, k=2 (over budget) | 66.86 | 0.714 | 1.000 | 0.857 | 0.992 | 0.242 | 0.908 | 1.000 | 1.000 | 1.000 |
| g, T=3 (b T=3), k=1 (over budget) | 16.61 | 0.760 | 0.938 | 0.849 | 0.998 | 0.312 | 0.971 | 0.812 | 1.000 | 1.000 |
| m, T=3, k=1 (over budget) | 16.61 | 0.760 | 0.931 | 0.845 | 0.998 | 0.312 | 0.971 | 0.792 | 1.000 | 1.000 |
| g, T=2 (b T=3), k=3 (over budget) | 23.64 | 0.718 | 0.968 | 0.843 | 0.998 | 0.167 | 0.990 | 0.904 | 1.000 | 1.000 |
| m, T=2, k=3 (over budget) | 23.64 | 0.718 | 0.967 | 0.842 | 0.998 | 0.167 | 0.990 | 0.900 | 1.000 | 1.000 |
| g, T=3 (b T=3), k=2 (over budget) | 12.36 | 0.738 | 0.919 | 0.829 | 0.998 | 0.263 | 0.954 | 0.758 | 1.000 | 1.000 |
| e, T=2, k=2 (over budget) | 55.43 | 0.833 | 0.821 | 0.827 | 0.998 | 0.502 | 1.000 | 0.537 | 0.925 | 1.000 |
| m, T=3, k=2 (over budget) | 12.36 | 0.738 | 0.914 | 0.826 | 0.998 | 0.263 | 0.954 | 0.742 | 1.000 | 1.000 |
| b, T=2, k=1 (over budget) | 21.61 | 0.650 | 0.994 | 0.822 | 0.998 | 0.185 | 0.767 | 0.983 | 1.000 | 1.000 |
| b, T=1.5, k=3 (over budget) | 37.39 | 0.633 | 1.000 | 0.817 | 0.979 | 0.060 | 0.860 | 1.000 | 1.000 | 1.000 |
| b, T=2, k=2 (over budget) | 18.46 | 0.628 | 0.989 | 0.808 | 0.994 | 0.144 | 0.746 | 0.967 | 1.000 | 1.000 |
| f, T=3 (b T=3), k=1 (over budget) | 11.96 | 0.772 | 0.831 | 0.801 | 1.000 | 0.327 | 0.990 | 0.496 | 0.996 | 1.000 |
| g, T=4 (b T=3), k=1 (over budget) | 4.96 | 0.703 | 0.881 | 0.792 | 0.998 | 0.233 | 0.879 | 0.642 | 1.000 | 1.000 |
| f, T=3 (b T=3), k=2 (over budget) | 7.43 | 0.754 | 0.811 | 0.783 | 0.998 | 0.281 | 0.983 | 0.446 | 0.988 | 1.000 |
| g, T=4 (b T=3), k=2 (over budget) | 4.11 | 0.694 | 0.865 | 0.780 | 0.998 | 0.219 | 0.867 | 0.596 | 1.000 | 1.000 |
| f, T=2 (b T=3), k=3 (over budget) | 21.68 | 0.738 | 0.812 | 0.775 | 0.998 | 0.221 | 0.996 | 0.446 | 0.992 | 1.000 |
| f, T=4 (b T=3), k=1 (over budget) | 2.82 | 0.733 | 0.810 | 0.771 | 0.998 | 0.248 | 0.952 | 0.438 | 0.992 | 1.000 |
| m, T=4, k=1 (over budget) | 4.71 | 0.703 | 0.836 | 0.770 | 0.998 | 0.233 | 0.879 | 0.533 | 0.975 | 1.000 |
| b, T=2, k=3 (over budget) | 6.86 | 0.570 | 0.969 | 0.770 | 0.988 | 0.023 | 0.700 | 0.908 | 1.000 | 1.000 |
| f, T=4 (b T=3), k=2 (over budget) | 2.07 | 0.724 | 0.796 | 0.760 | 0.998 | 0.229 | 0.944 | 0.404 | 0.983 | 1.000 |

Verdict: the best rule is f, T=6 (b T=3), k=1 (mean recall 0.730 vs 0.622 for the best b at <=1/h); families that beat rule b on dev under the budget: m, f, g. Development only; a rule using the AE must still be confirmed on the fresh test block.


## Phase C1 re-run with the corrected input quantiser (both dev tables recorded)

`ae.h` now quantises with `round_half_away(v / scale)` (TFLite 2.21 reference QUANTIZE) instead of `v * (1/scale)`. The tables above are the RE-RUN (new quantiser); the previous run's tables are kept unchanged in `old_quantiser/` (`fusion_eval_r3_dev.md/.json`, `fusion_eval_r3_selected.json`). The recordings were re-simulated (deterministic, same seeds), the AE scores recomputed from the C code, and the identical pre-registered selection rule re-run.

Result: **the selection did not change** (old: f, T=6 (b T=3), k=1; new: f, T=6 (b T=3), k=1) and all 165 table rows are identical in false onsets/h and mean recall (the two quantiser forms differ only at near-half values, about 1% of adversarial values and essentially never on real windows). The configuration scored on the fresh block is therefore the one in the pre-registration.
