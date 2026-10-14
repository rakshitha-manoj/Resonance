# Model comparison (Phase 3 follow-up)

## Purpose

Check whether any model other than the selected dt_d3 (DecisionTree depth 3, 18 inputs `{axis}_{std,kurt,domfreq}`) is clearly better, using the same broad resampling as the Phase 3 headline. Scripts: `models/training/compare.py` (18 DT/RF configs) and `models/training/compare_svm.py` (6 SVMs). Raw outputs: `models/selected/comparison/` (results.csv/json, svm_results.csv/json).

## Pre-registered rule (stated before results)

Replace dt_d3 only if a config satisfies ALL of:

1. footprint <= 100 KB
2. paired mean accuracy difference vs dt_d3 >= +0.010 in (a) 200 holdout re-draws (10 recordings/class test, 40/class train, seeds 42+5000+i)
3. paired mean difference >= +0.010 in (b) 40 repeats of grouped stratified 5-fold over all 150 recordings (seeds 42+6000+i)
4. (b) win rate >= 0.75 (fraction of repeats where the config is at least as accurate as dt_d3)
5. (a) 5th percentile >= dt_d3's (0.8841)

RF uses hard majority vote (ties to lowest class index). Footprint for trees is 10 B/node (mean over all fits); linear SVM is float32 weights+scaler; RBF SVM is support vectors x 18 x 4 B + coefficients + scaler (largest fit shown).

## Results (all 24 configs, sorted by footprint within family)

| config | footprint B | (a) mean | (a) p5 | (b) mean | (a) diff | (b) diff | (b) win rate | qualifies |
|---|---|---|---|---|---|---|---|---|
| dt_d3 (reference) | 116 | 0.9315 | 0.8841 | 0.9315 | - | - | - | reference |
| dt_d4 | 181 | 0.9242 | 0.8688 | 0.9259 | -0.0074 | -0.0057 | 0.275 | no |
| dt_d5 | 273 | 0.9147 | 0.8325 | 0.9189 | -0.0169 | -0.0127 | 0.050 | no |
| dt_d6 | 379 | 0.8987 | 0.8100 | 0.9038 | -0.0329 | -0.0278 | 0.025 | no |
| dt_d7 | 495 | 0.8834 | 0.7933 | 0.8933 | -0.0481 | -0.0383 | 0.000 | no |
| dt_d8 | 624 | 0.8761 | 0.7791 | 0.8810 | -0.0555 | -0.0505 | 0.000 | no |
| rf_n5_d4 | 1,118 | 0.8874 | 0.8174 | 0.8890 | -0.0441 | -0.0425 | 0.000 | no |
| rf_n10_d4 | 2,380 | 0.9084 | 0.8558 | 0.9103 | -0.0231 | -0.0212 | 0.000 | no |
| rf_n5_d6 | 2,652 | 0.9181 | 0.8566 | 0.9203 | -0.0135 | -0.0113 | 0.175 | no |
| rf_n20_d4 | 4,457 | 0.9249 | 0.8781 | 0.9270 | -0.0067 | -0.0046 | 0.250 | no |
| rf_n5_d8 | 4,599 | 0.9152 | 0.8525 | 0.9185 | -0.0164 | -0.0130 | 0.150 | no |
| rf_n10_d6 | 5,306 | 0.9310 | 0.8824 | 0.9319 | -0.0005 | +0.0003 | 0.500 | no |
| rf_n10_d8 | 9,076 | 0.9295 | 0.8758 | 0.9320 | -0.0021 | +0.0005 | 0.500 | no |
| rf_n20_d6 | 10,267 | 0.9325 | 0.8800 | 0.9340 | +0.0010 | +0.0024 | 0.675 | no |
| rf_n50_d4 | 11,346 | 0.9377 | 0.8966 | 0.9403 | +0.0061 | +0.0087 | 0.925 | no |
| rf_n20_d8 | 17,600 | 0.9322 | 0.8783 | 0.9354 | +0.0006 | +0.0038 | 0.775 | no |
| rf_n50_d6 | 25,434 | 0.9392 | 0.8950 | 0.9416 | +0.0077 | +0.0100 | 0.925 | no |
| rf_n50_d8 | 43,482 | 0.9378 | 0.8900 | 0.9411 | +0.0062 | +0.0096 | 0.925 | no |
| svm_lin_c0.1 | 372 | 0.9075 | 0.8525 | 0.9066 | -0.0241 | -0.0249 | 0.025 | no |
| svm_lin_c1 | 372 | 0.9040 | 0.8431 | 0.9033 | -0.0276 | -0.0283 | 0.025 | no |
| svm_lin_c10 | 372 | 0.9026 | 0.8341 | 0.9022 | -0.0289 | -0.0294 | 0.025 | no |
| svm_rbf_c1 | 89,036 | 0.9109 | 0.8574 | 0.9119 | -0.0206 | -0.0197 | 0.025 | no |
| svm_rbf_c10 | 58,156 | 0.8919 | 0.8266 | 0.8934 | -0.0396 | -0.0382 | 0.025 | no |
| svm_rbf_c100 | 38,476 | 0.8737 | 0.8011 | 0.8763 | -0.0579 | -0.0552 | 0.000 | no |

Footprint of dt_d3 on all 150 recordings: 110 B (11 nodes). Win rate is only relevant for the (b) condition; (a) win rates are in results.csv.

## MLP

Not re-run here. In train.py, the best MLP (mlp_8) reached about 0.87 on dev repeated CV (0.8724), and int8 simulation did not change that; all four MLP configs were below 0.90 and ineligible.

## Outcome

No config qualifies. dt_d3 stays. Closest: rf_n50_d6 passes the (b) diff condition by only +1.7e-5 (+0.010017) but fails (a) at +0.0077. SVMs are all worse than dt_d3 (best svm_rbf_c1 at -0.0206 in (a)) and RBF is also large and expensive at inference.

## Documented fallback: rf_n50_d4

If dt_d3 falls short on real hardware data: 50 trees depth 4, 11,520 B on 150 recordings (1152 nodes), (a) 0.9377 (+0.0061), (b) 0.9403 (+0.0087), (b) win rate 0.925, (a) p5 0.8966. Costs: 11.5 KB (about 100x dt_d3), uses all 18 features and all 6 FFT axes, roughly 3x the feature-extraction work of dt_d3 (which needs FFT on only 2 axes), plus up to 50 tree traversals per window.

## Model types not tried

- 1D CNN and LSTM/GRU: need a runtime (TFLM or hand-written kernels) that the hand-written-C deployment plan avoids, and learning from the raw synthetic signal risks fitting generator artifacts rather than structural behaviour. Revisit a CNN in Phase 6 if trees fall short on real data.

## Note on the deployed model

These comparisons evaluate the training procedure on 120-recording training sets (the training side of each re-draw / fold), which is consistent with the deployed dt_d3 being retrained on all 150 recordings. The deployed model has no untouched evaluation data; Phase 6 hardware data is its real test.
