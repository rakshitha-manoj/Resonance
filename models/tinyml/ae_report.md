# int8 autoencoder (Phase B): report

Healthy-data-only anomaly detector for each sensor node. All numbers: simulation (`building_sim`), nothing measured on hardware.

## Model

- architecture 20-12-2-12-20 (ReLU, dense), design A input (16 az log band-powers, az_std, az_kurt, gx_std, gx_kurt), per-node normalisation learned on the node from the 20 commissioning windows (median / 1.4826 MAD, floor = 0.25 x the training median MAD per input)
- parameters 574; int8 TFLite file 4568 bytes; firmware weights+quantisation tables 942 bytes (528 int8 MACs per window)
- training: seeds 6,300,000-6,300,399 (400 buildings x 10 floors; commissioning + default and broadband ambient at level 0.5-1.5x, 220 windows per node, 880,000 windows), validation 6,300,400-6,300,499; Adam, MSE, 30 epochs; full-integer post-training quantisation, 3000 representative training vectors
- score = MSE in normalised space; ratio = score / median score of the 20 commissioning windows (the ratio is what is thresholded)

## Float vs int8 (validation healthy windows)

- reconstruction MSE: float 2.2541, int8 2.3487 (+4.2%); median 1.6219 vs 1.6901
- per-window score: mean relative difference 5.7%, correlation 0.9899
- quantisation degradation of detection (validation, window level at the healthy 0.5% false-positive threshold): mean recall float 0.608 vs int8 0.600 (impacts 0.930/0.931, sparse 0.791/0.781, shift 0.609/0.596, x3 0.101/0.093)
- C kernel vs `tf.lite.Interpreter` (BUILTIN_REF): 0 mismatching int8 outputs on 10,000 vectors (200,000 values); the reference interpreter and BUILTIN_WITHOUT_DEFAULT_DELEGATES agree, but the default (XNNPACK) resolver differed from the reference in 1 of 200,000 outputs (and its QUANTIZE rounds half to even, so it is not the reference). TFLite in this build rounds ONCE in MultiplyByQuantizedMultiplier (single-rounding form); the gemmlowp double-rounding form differs in 28% of the vectors.

## Input design and architecture (validation split only, `design_search.json`)

Window-level recall of the anomaly windows at the threshold that gives 0.5% false positives on healthy validation windows (levels 0.5-1.5x); mean of impact / sparse impact / frequency shift / 3x building-wide; two training seeds each.

| design | hidden | seed | params | healthy p99 ratio | impact | sparse | shift | 3x | mean |
|---|---|---|---|---|---|---|---|---|---|
| A | [12, 4, 12] | 0 | 624 | 4.27 | 0.91 | 0.75 | 0.60 | 0.13 | 0.599 |
| A | [12, 4, 12] | 1 | 624 | 4.91 | 0.93 | 0.79 | 0.46 | 0.09 | 0.567 |
| B | [12, 4, 12] | 0 | 574 | 4.90 | 0.84 | 0.76 | 0.21 | 0.02 | 0.457 |
| B | [12, 4, 12] | 1 | 574 | 4.90 | 0.84 | 0.76 | 0.32 | 0.02 | 0.483 |
| C | [12, 4, 12] | 0 | 599 | 4.96 | 0.86 | 0.77 | 0.24 | 0.02 | 0.475 |
| C | [12, 4, 12] | 1 | 599 | 5.01 | 0.85 | 0.77 | 0.22 | 0.01 | 0.466 |
| D | [12, 4, 12] | 0 | 624 | 4.85 | 0.93 | 0.78 | 0.33 | 0.10 | 0.535 |
| D | [12, 4, 12] | 1 | 624 | 4.89 | 0.93 | 0.78 | 0.39 | 0.12 | 0.558 |
| A | [8, 3, 8] | 0 | 407 | 4.59 | 0.93 | 0.80 | 0.68 | 0.10 | 0.628 |
| A | [8, 3, 8] | 1 | 407 | 4.72 | 0.93 | 0.79 | 0.55 | 0.09 | 0.592 |
| A | [16, 6, 16] | 0 | 890 | 5.16 | 0.93 | 0.77 | 0.16 | 0.09 | 0.488 |
| A | [16, 6, 16] | 1 | 890 | 5.06 | 0.93 | 0.76 | 0.13 | 0.09 | 0.477 |
| A | [24, 8, 24] | 0 | 1420 | 5.04 | 0.90 | 0.73 | 0.13 | 0.13 | 0.470 |
| A | [24, 8, 24] | 1 | 1420 | 5.13 | 0.90 | 0.73 | 0.13 | 0.11 | 0.470 |
| A | [12, 2, 12] | 0 | 574 | 4.47 | 0.93 | 0.80 | 0.67 | 0.10 | 0.627 |
| A | [12, 2, 12] | 1 | 574 | 4.72 | 0.93 | 0.78 | 0.60 | 0.09 | 0.602 |
| A | [12, 6, 12] | 0 | 674 | 5.15 | 0.93 | 0.77 | 0.18 | 0.09 | 0.492 |
| A | [12, 6, 12] | 1 | 674 | 4.38 | 0.93 | 0.77 | 0.32 | 0.17 | 0.544 |

Designs: A = the 20 base inputs; B = shape only (band log-powers minus their mean, + the two kurtoses); C = B + log(gx_std/az_std); D = A with log std.
Findings: (1) the shape-only designs (B, C) did NOT lower the healthy false-positive tail (p99 ratio 4.9-5.0 vs 4.3-4.9 for A) and lost most of the frequency-shift and amplitude sensitivity, so design A was kept. (2) Smaller bottlenecks and hidden layers generalise less to the anomalies: 16-6-16 and 24-8-24 are clearly worse (mean 0.47-0.49), 8-3-8 / 12-2-12 / 12-4-12 are within the seed noise (0.58-0.63); 12-2-12 had the best two-seed mean and was taken. (3) Because the training data contains the 0.5-1.5x level changes, a building-wide amplitude change is largely reconstructed (3x recall 0.1): the AE does not replace rule b for amplitude events.

Mahalanobis baseline on the same normalised inputs: covariance of the 880,000 training windows, shrinkage towards (trace/n) I chosen on validation = 0.8, validation mean recall 0.699 (AE int8 0.600).

## Benchmark (development block 6,100,000+240, primary pair, best configuration of each family under 1.0 false onsets/h)

| detector | params | flash bytes (weights) | RAM | ops per window | host time per window | dev config | false onsets/h | LOCAL | BUILDING | shift | sparse |
|---|---|---|---|---|---|---|---|---|---|---|---|
| decision tree (depth 3, rule a, lowest-false-onset row) | 11 nodes | 110 | 5 floats | ~3 compares (+ 2 DFTs of the front end, 32,768 MAC) | features5 33.5 us | a, T=1.5, smoothed, k=3 | 8.86 | 0.676 | 0.536 | 0.975 | 0.060 |
| rule b (az_std / baseline >= T, fusion) | 1 baseline | 4 | 4 B baseline + 3 flags | 1 divide + compare (+ std pass, ~400 flops) | < 1 us (std only) | b, T=3, k=1 | 0.79 | 0.442 | 0.801 | 0.294 | 0.054 |
| Mahalanobis (shrinkage 0.80), rule m | 20 + 210 | 920 | ~1.8 KB node state (shared with AE) | ~440 MAC (float) | 1.98 us (numpy batch) | m, T=4, k=3 | 0.96 | 0.618 | 0.737 | 0.848 | 0.013 |
| int8 autoencoder 20-12-2-12-20, rule e | 574 | 942 | ~1.8 KB node state + 40 B scratch | 528 int8 MAC | 1.41 us (norm+quant+AE+score) | e, T=5, k=1 | 0.43 | 0.692 | 0.382 | 0.867 | 0.212 |
| rule f = b OR e | - | 946 | as AE | 528 int8 MAC + b | as e | f, T=6 (b T=3), k=1 | 0.89 | 0.658 | 0.803 | 0.771 | 0.204 |
| rule g = b OR m | - | 924 | as m | as m + b | as m | g, T=4 (b T=3), k=3 | 1.00 | 0.617 | 0.779 | 0.848 | 0.010 |

Host: front end (az DFT, 16 bands, std/kurt) 18.4 us per window; features5 (tree features, two DFTs) 33.5 us. ESP32 timings: see the `ae us` line of the selftest (Wokwi / hardware). The tree row shows the lowest-false-onset rule-a configuration of R2b (the tree-based rules do not meet the budget). Dev metrics are from `models/selected/validation/fusion_eval_r3_dev.md`.
