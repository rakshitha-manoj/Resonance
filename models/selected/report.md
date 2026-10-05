# Resonance Phase 3 report

## Headline: dt_d3 accuracy (broad resampling)

- 200 random holdout re-draws (10 rec/class test, 40 rec/class train, all 150 recordings): mean 0.9315, sd 0.0281, min 0.8417, 176/200 >= 0.90
- 40 repeats of grouped stratified 5-fold CV over all 150 recordings: mean 0.9315, sd 0.0075, min 0.9057
- Both estimates use the 30 set-aside recordings and are therefore NOT selection-free.
- Supporting evidence: fixed set-aside holdout 0.9383 (cm [[387, 0, 13], [30, 370, 0], [26, 5, 369]]). The set-aside recordings were part of the evidence that drove the switch from rf_n20_d4 (0.9042) to dt_d3, so this is NOT an untouched holdout estimate.
- Dev-only 10x repeated CV (the data used for selection): mean 0.9220, sd 0.0120, min 0.9087.

## Deployed model

- dt_d3 retrained on ALL 150 recordings (6000 windows), 18 input features; this is the model in model.joblib / model_export.json.
- structure: 11 nodes, 110 B deployed (dev-trained dt_d3 was 13 nodes, 130 B).
- used features (5): az_std, az_kurt, az_domfreq, gx_std, gx_domfreq
- axes needed: az, gx; FFT axes: az, gx
- It has no untouched evaluation data. Its accuracy estimate is the broad-resampling estimate of the training procedure above (about 0.93); Phase 6 hardware data is its real test.
- The fixed-holdout metrics, family-representative metrics and offset test in this report describe the dev-trained model (120 recordings), not the deployed model.
- Alternatives and the documented fallback rf_n50_d4: see comparison.md.

## Selection history

- v1: dt_d4, 24 features, single 5-fold fold assignment, rule: smallest footprint with CV mean >= 0.90. Changed because: single-assignment luck (dt_d3 missed 0.90 at 0.8975 only on that one fold assignment) and az_rms offset fragility (24-feature trees split on DC-dominated az_rms; rms dropped).
- v2: rf_n20_d4, 18 features, 10x repeated grouped 5-fold on dev, rule: highest mean among configs <= 5120 B with all repeat means >= 0.9. Changed because: the RF dev-CV lead did not survive broad resampling (rf_n20_d4 0.9249 vs dt_d3 0.9315 over 200 re-draws; 0.9270 vs 0.9315 over 40 all-data CV repeats; dt_d3 >= rf_n20_d4 in 75% of CV repeats; fixed holdout 0.9042 vs 0.9383); RF hard-vote ties break toward normal by the lowest-index rule; rf_n20_d4 is 33x the size.
- v3: dt_d3, 18 features, 10x repeated grouped 5-fold on dev, rule: smallest deployed footprint among configs <= 5120 B with all repeat means >= 0.9; ties -> higher mean. (current selection)
- v4: dt_d3, 18 features, none (no selection change), rule: deployment: same config retrained on all 150 recordings. (current deployed model; not a selection change)

## Scope of the offset test

The offset test shows the 18 mean-removed features are invariant to constant per-axis offsets BY CONSTRUCTION. It does NOT test axis rotation / mounting orientation changes, sensitivity/gain (scale) errors, or temperature drift; these are open questions for Phase 6 hardware validation.

## Broad resampling (all eligible configs; uses set-aside recordings, NOT selection-free)

Re-draws: 200 seeds (42+5000+i); per seed 10 recordings/class test from all 50, train on remaining 40/class; RF by hard vote. All-data CV: 40 repeats (seed 42+6000+i) of grouped stratified 5-fold over all 150 recordings. Diff = config minus winner (paired); winner>= = fraction of re-draws/repeats where the winner is at least as accurate.

| config | deployed B | redraw mean | sd | min | >=0.90 | diff vs winner | winner>= | allCV mean | sd | min | diff vs winner | winner>= |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| dt_d3 (winner) | 130 | 0.9315 | 0.0281 | 0.8417 | 176/200 | +0.0000 | 1.00 | 0.9315 | 0.0075 | 0.9057 | +0.0000 | 1.00 |
| dt_d4 | 210 | 0.9242 | 0.0338 | 0.7842 | 159/200 | -0.0074 | 0.43 | 0.9259 | 0.0100 | 0.8958 | -0.0057 | 0.72 |
| rf_n5_d6 | 2590 | 0.9181 | 0.0329 | 0.7883 | 147/200 | -0.0135 | 0.68 | 0.9203 | 0.0083 | 0.9047 | -0.0113 | 0.82 |
| rf_n20_d4 | 4320 | 0.9249 | 0.0299 | 0.8358 | 158/200 | -0.0067 | 0.64 | 0.9270 | 0.0066 | 0.9057 | -0.0046 | 0.75 |

## Previous winner rf_n20_d4 (fixed holdout)

- holdout acc 0.9042, cm [[398, 0, 2], [83, 317, 0], [25, 5, 370]]
- rf_n20_d4 hard-vote ties on the fixed holdout: 8 of 1200 windows; true classes of tied windows {'normal': 0, 'abnormal': 8, 'faulty': 0}, predicted {'normal': 6, 'abnormal': 2, 'faulty': 0} (lowest-index tie rule breaks toward normal).

Band energy used: False
Input features: 18 (ax_std, ax_kurt, ax_domfreq, ay_std, ay_kurt, ay_domfreq, az_std, az_kurt, az_domfreq, gx_std, gx_kurt, gx_domfreq, gy_std, gy_kurt, gy_domfreq, gz_std, gz_kurt, gz_domfreq)

## Split

- dev: normal 40 rec / 1600 win, abnormal 40 rec / 1600 win, faulty 40 rec / 1600 win
- holdout: normal 10 rec / 400 win, abnormal 10 rec / 400 win, faulty 10 rec / 400 win
- 10 repeats of grouped stratified 5-fold on dev (fold seeds 43..52); every fold: train 32 rec/class (1280 win/class), test 8 rec/class (320 win/class); fold ids in report.json

## Configs (repeated CV on dev; eligible = footprint <= 5120 B and all 10 repeat means >= 0.9)

| config | deployed B | mean | sd | min | repeats >= 0.90 | eligible |
|---|---|---|---|---|---|---|
| dt_d3 | 130 | 0.9220 | 0.0120 | 0.9087 | 10/10 | True |
| dt_d4 | 210 | 0.9226 | 0.0150 | 0.9012 | 10/10 | True |
| dt_d5 | 330 | 0.9165 | 0.0178 | 0.8817 | 8/10 | False |
| dt_d6 | 390 | 0.9056 | 0.0203 | 0.8638 | 7/10 | False |
| dt_d7 | 490 | 0.8951 | 0.0214 | 0.8683 | 3/10 | False |
| dt_d8 | 610 | 0.8779 | 0.0199 | 0.8425 | 1/10 | False |
| rf_n5_d4 | 1130 | 0.8940 | 0.0132 | 0.8719 | 2/10 | False |
| rf_n5_d6 | 2590 | 0.9206 | 0.0095 | 0.9094 | 10/10 | True |
| rf_n5_d8 | 4230 | 0.9167 | 0.0141 | 0.8904 | 8/10 | False |
| rf_n10_d4 | 2280 | 0.9116 | 0.0127 | 0.8867 | 9/10 | False |
| rf_n10_d6 | 5220 | 0.9383 | 0.0058 | 0.9285 | 10/10 | False |
| rf_n10_d8 | 8300 | 0.9330 | 0.0079 | 0.9169 | 10/10 | False |
| rf_n20_d4 | 4320 | 0.9330 | 0.0097 | 0.9190 | 10/10 | True |
| rf_n20_d6 | 10120 | 0.9399 | 0.0084 | 0.9237 | 10/10 | False |
| rf_n20_d8 | 16920 | 0.9389 | 0.0076 | 0.9225 | 10/10 | False |
| rf_n50_d4 | 10860 | 0.9443 | 0.0083 | 0.9300 | 10/10 | False |
| rf_n50_d6 | 24620 | 0.9435 | 0.0082 | 0.9267 | 10/10 | False |
| rf_n50_d8 | 41460 | 0.9413 | 0.0094 | 0.9227 | 10/10 | False |
| mlp_8 | 364 | 0.8724 | 0.0140 | 0.8519 | 0/10 | False |
| mlp_16 | 564 | 0.8474 | 0.0105 | 0.8277 | 0/10 | False |
| mlp_32 | 964 | 0.8460 | 0.0105 | 0.8306 | 0/10 | False |
| mlp_16x16 | 888 | 0.8365 | 0.0163 | 0.8010 | 0/10 | False |

## Family representatives (OOF pooled over all repeats; holdout afterwards)

### tree: dt_d3
- CV mean 0.9220, sd 0.0120, min 0.9087, 10/10 repeats >= 0.90, deployed 130 B, eligible True
- OOF acc 0.9220, precision {'normal': 0.861089, 'abnormal': 0.942498, 'faulty': 0.974927}, recall {'normal': 0.960438, 'abnormal': 0.928125, 'faulty': 0.877313}, cm [[15367, 556, 77], [866, 14850, 284], [1613, 350, 14037]]
- holdout acc 0.9383, precision {'normal': 0.873589, 'abnormal': 0.986667, 'faulty': 0.965969}, recall {'normal': 0.9675, 'abnormal': 0.925, 'faulty': 0.9225}, cm [[387, 0, 13], [30, 370, 0], [26, 5, 369]]

### forest: rf_n5_d6
- CV mean 0.9206, sd 0.0095, min 0.9094, 10/10 repeats >= 0.90, deployed 2590 B, eligible True
- OOF acc 0.9206, precision {'normal': 0.855531, 'abnormal': 0.934875, 'faulty': 0.983643}, recall {'normal': 0.947875, 'abnormal': 0.904375, 'faulty': 0.909563}, cm [[15166, 721, 113], [1401, 14470, 129], [1160, 287, 14553]]
- holdout acc 0.9292, precision {'normal': 0.861233, 'abnormal': 0.983333, 'faulty': 0.958549}, recall {'normal': 0.9775, 'abnormal': 0.885, 'faulty': 0.925}, cm [[391, 0, 9], [39, 354, 7], [24, 6, 370]]
- holdout vote ties: 27 of 1200 windows

### mlp: mlp_8
- CV mean 0.8724, sd 0.0140, min 0.8519, 0/10 repeats >= 0.90, deployed 364 B, eligible False
- OOF acc 0.8724, precision {'normal': 0.863957, 'abnormal': 0.905521, 'faulty': 0.850254}, recall {'normal': 0.819625, 'abnormal': 0.876375, 'faulty': 0.92125}, cm [[13114, 1125, 1761], [1143, 14022, 835], [922, 338, 14740]]
- holdout acc 0.8683, precision {'normal': 0.801688, 'abnormal': 0.972696, 'faulty': 0.87067}, recall {'normal': 0.95, 'abnormal': 0.7125, 'faulty': 0.9425}, cm [[380, 4, 16], [75, 285, 40], [19, 4, 377]]
- holdout int8-simulated accuracy 0.8692

## Winner: dt_d3 (selection; dev-trained numbers)
- dev CV mean 0.9220, sd 0.0120, min 0.9087, fixed set-aside holdout 0.9383 (not untouched), deployed 130 B
- deployed (all-data) model used features (5): az_std, az_kurt, az_domfreq, gx_std, gx_domfreq
- firmware must compute: {'az': ['std', 'kurt', 'domfreq'], 'gx': ['std', 'domfreq']}

## Justification

- Winner dt_d3: smallest deployed footprint (130 B, limit 5120 B) among 4 eligible configs (eligible = footprint <= 5120 B and all 10 dev repeat means >= 0.9); dev repeated-CV mean 0.9220, sd 0.0120, min repeat 0.9087, 10/10 repeats >= 0.9.
- Eligible dt_d4: 210 B (+80 B vs winner), dev CV mean 0.9226 (+0.0006).
- Eligible rf_n5_d6: 2590 B (+2460 B vs winner), dev CV mean 0.9206 (-0.0014).
- Eligible rf_n20_d4: 4320 B (+4190 B vs winner), dev CV mean 0.9330 (+0.0110).
- vs forest representative rf_n5_d6: +2460 B, -0.0014 dev CV mean, min repeat 0.9094, 10/10 repeats >= 0.9, eligible True.
- vs mlp representative mlp_8: +234 B, -0.0495 dev CV mean, min repeat 0.8519, 0/10 repeats >= 0.9, eligible False.
- Broad resampling (uses set-aside recordings, NOT selection-free): dt_d3 0.9315 over 200 holdout re-draws, 0.9315 over 40 all-data CV repeats.
- rf_n20_d4 vs dt_d3: re-draws 0.9249 vs 0.9315; all-data CV 0.9270 vs 0.9315; dt_d3 >= rf_n20_d4 in 75% of CV repeats.
- Fixed set-aside holdout for dt_d3: 0.9383 (supporting evidence only; these recordings helped drive the switch from rf_n20_d4 (0.9042), so it is not an untouched holdout estimate).
- Offset test (holdout, +/-1000 count per-axis offsets): winner mean drop 0.0000, max drop 0.0000, worst az +/-1000 drop 0.0000. Scope: see offset_scope.

## Offset robustness (holdout)

Per-recording offset U(-1000,1000) counts on every axis, 10 draws; az worst case = constant -1000 / +1000 on az. The offset test shows the 18 mean-removed features are invariant to constant per-axis offsets BY CONSTRUCTION. It does NOT test axis rotation / mounting orientation changes, sensitivity/gain (scale) errors, or temperature drift; these are open questions for Phase 6 hardware validation.

| model | features | unperturbed | random mean | random min | drop mean | drop max | az -1000 | az +1000 |
|---|---|---|---|---|---|---|---|---|
| dt_d3 (winner) | 18 | 0.9383 | 0.9383 | 0.9383 | 0.0000 | 0.0000 | 0.9383 | 0.9383 |
| rf_n5_d6 (rep forest) | 18 | 0.9292 | 0.9292 | 0.9292 | -0.0000 | 0.0000 | 0.9292 | 0.9292 |
| mlp_8 (rep mlp) | 18 | 0.8683 | 0.8683 | 0.8683 | -0.0000 | 0.0000 | 0.8683 | 0.8683 |
| dt_d4 | 18 | 0.9300 | 0.9300 | 0.9300 | 0.0000 | 0.0000 | 0.9300 | 0.9300 |
| rf_n20_d4 | 18 | 0.9042 | 0.9042 | 0.9042 | 0.0000 | 0.0000 | 0.9042 | 0.9042 |
| dt_d3 +rms | 24 | 0.9383 | 0.9383 | 0.9383 | 0.0000 | 0.0000 | 0.9383 | 0.9383 |
| dt_d4 +rms | 24 | 0.9275 | 0.8053 | 0.7467 | 0.1222 | 0.1808 | 0.6300 | 0.9275 |
| rf_n20_d4 +rms | 24 | 0.9333 | 0.9357 | 0.9317 | -0.0024 | 0.0017 | 0.9425 | 0.9333 |

## Footprint formulas

- tree: bytes = 10 * node_count (feature idx u8 + threshold f32 + left int16 + right int16 + class/leaf u8); int16-threshold variant = 8 * node_count; forest = sum over trees; ops = sum of max tree depths
- mlp: int8_bytes = weights*1 + biases*4 + layers*4 (one f32 scale per layer) + n_features*8 (scaler mean+scale f32); ops = MAC count = weights
- forest_inference: one class per leaf, hard majority vote, ties -> lowest class index (all forest accuracies use this); single tree: leaf class
