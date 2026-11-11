# T1b: retraining on simulated-building data

**RETRAIN: KEEP OLD**

## Limits

- Results describe fit to the simulator and say nothing about real structures.
- gx is modelled, not physical (gain x lateral signal + noise + offset).
- The first-mode range 4-10 Hz is a scale model's.
- Excitation is stationary band-limited noise.
- The decision rule and design are fixed in `models/training/train_building.py`; this is not described as pre-registered.

## Replacement rule

new tree replaces the old only if its decisive false alarm rate (mean over floors, healthy ambient + noise-only windows, after 2-of-3 smoothing) is lower than the old tree+gate's AND its local-event detection (event-floor event windows reported faulty after smoothing) is >= the old tree+gate's; no margin

## Data

- development buildings 240, held-out buildings 150 (seeds (4100000, 240), (4200000, 150); all above 4,000,000 and disjoint from earlier gate blocks, asserted)
- dev pool windows 288000: {'normal': 214332, 'abnormal': 57600, 'faulty': 16068}; by subtype {'healthy': 28800, 'noise': 28800, 'event_scenario_normal': 156732, 'strong': 57600, 'faulty_impact': 7428, 'faulty_shift': 8640}
- balanced training windows 48204: {'normal': 16068, 'abnormal': 16068, 'faulty': 16068}; by subtype {'healthy': 5356, 'noise': 5356, 'event_scenario_normal': 5356, 'strong': 16068, 'faulty_impact': 7428, 'faulty_shift': 8640}
- class weights: none (classes balanced by construction: all faulty windows kept, equal numbers of abnormal and normal sampled)

## Selection (development buildings only, grouped repeated 5-fold)

### Feature set A: az_std, az_kurt, az_domfreq, gx_std, gx_kurt, gx_domfreq

| config | nodes | bytes | cv mean | min repeat | repeats>=0.90 | eligible |
|---|---|---|---|---|---|---|
| dt_d3 | 15 | 150 | 0.8572 | 0.8564 | 0 | False |
| dt_d4 | 27 | 270 | 0.8589 | 0.8581 | 0 | False |
| dt_d5 | 51 | 510 | 0.8689 | 0.8683 | 0 | False |
| dt_d6 | 95 | 950 | 0.8721 | 0.8712 | 0 | False |
| dt_d7 | 167 | 1670 | 0.8770 | 0.8755 | 0 | False |
| dt_d8 | 271 | 2710 | 0.8787 | 0.8775 | 0 | False |
| rf_n5_d4 | 153 | 1530 | 0.8550 | 0.8540 | 0 | False |
| rf_n5_d6 | 515 | 5150 | skipped (over 5120 B) | - | - | False |
| rf_n5_d8 | 1423 | 14230 | skipped (over 5120 B) | - | - | False |
| rf_n10_d4 | 304 | 3040 | 0.8588 | 0.8573 | 0 | False |
| rf_n10_d6 | 996 | 9960 | skipped (over 5120 B) | - | - | False |
| rf_n10_d8 | 2790 | 27900 | skipped (over 5120 B) | - | - | False |
| rf_n20_d4 | 604 | 6040 | skipped (over 5120 B) | - | - | False |
| rf_n20_d6 | 2026 | 20260 | skipped (over 5120 B) | - | - | False |
| rf_n20_d8 | 5630 | 56300 | skipped (over 5120 B) | - | - | False |
| rf_n50_d4 | 1514 | 15140 | skipped (over 5120 B) | - | - | False |
| rf_n50_d6 | 5158 | 51580 | skipped (over 5120 B) | - | - | False |
| rf_n50_d8 | 14358 | 143580 | skipped (over 5120 B) | - | - | False |

winner of set A: None

### Feature set B: az_std, az_kurt, az_domfreq, gx_std, gx_kurt, gx_domfreq, az_std_rel, gx_std_rel

| config | nodes | bytes | cv mean | min repeat | repeats>=0.90 | eligible |
|---|---|---|---|---|---|---|
| dt_d3 | 15 | 150 | 0.9141 | 0.9130 | 10 | True |
| dt_d4 | 29 | 290 | 0.9165 | 0.9157 | 10 | True |
| dt_d5 | 53 | 530 | 0.9326 | 0.9319 | 10 | True |
| dt_d6 | 91 | 910 | 0.9354 | 0.9348 | 10 | True |
| dt_d7 | 155 | 1550 | 0.9365 | 0.9353 | 10 | True |
| dt_d8 | 251 | 2510 | 0.9373 | 0.9361 | 10 | True |
| rf_n5_d4 | 153 | 1530 | 0.9130 | 0.9105 | 10 | True |
| rf_n5_d6 | 509 | 5090 | 0.9254 | 0.9218 | 10 | True |
| rf_n5_d8 | 1443 | 14430 | skipped (over 5120 B) | - | - | False |
| rf_n10_d4 | 308 | 3080 | 0.9146 | 0.9132 | 10 | True |
| rf_n10_d6 | 1020 | 10200 | skipped (over 5120 B) | - | - | False |
| rf_n10_d8 | 2786 | 27860 | skipped (over 5120 B) | - | - | False |
| rf_n20_d4 | 602 | 6020 | skipped (over 5120 B) | - | - | False |
| rf_n20_d6 | 2034 | 20340 | skipped (over 5120 B) | - | - | False |
| rf_n20_d8 | 5678 | 56780 | skipped (over 5120 B) | - | - | False |
| rf_n50_d4 | 1506 | 15060 | skipped (over 5120 B) | - | - | False |
| rf_n50_d6 | 5054 | 50540 | skipped (over 5120 B) | - | - | False |
| rf_n50_d8 | 14108 | 141080 | skipped (over 5120 B) | - | - | False |

winner of set B: dt_d3

Chosen: set B, dt_d3 (a config qualified: True); deployed footprint {'nodes': 15, 'deployed_bytes': 150, 'max_comparisons': 3}.

## Decision (held-out buildings)

| model | false alarm smoothed | false alarm raw | detection smoothed | detection raw | det. impact (sm) | det. shift (sm) |
|---|---|---|---|---|---|---|
| new | 0.051 | 0.064 | 0.879 | 0.894 | 0.891 | 0.868 |
| old + gate | 0.007 | 0.013 | 0.887 | 0.902 | 0.876 | 0.896 |
| old, no gate | 0.406 | 0.387 | 0.887 | 0.903 | 0.877 | 0.896 |

Bootstrap 95% intervals (resampling held-out buildings):

- false alarm (smoothed): new [0.039, 0.064], old+gate [0.004, 0.010], difference new-old [0.033, 0.056]
- detection (smoothed): new [0.858, 0.898], old+gate [0.868, 0.904], difference new-old [-0.025, 0.010]

False alarm by floor (smoothed), floors 1..10:

- new: 0.01 0.03 0.04 0.05 0.06 0.06 0.06 0.06 0.06 0.07
- old + gate: 0.00 0.00 0.00 0.00 0.00 0.01 0.01 0.01 0.02 0.02
- old, no gate: 0.41 0.40 0.39 0.39 0.40 0.41 0.41 0.41 0.41 0.42

False faulty on non-event floors during a local event (smoothed): new: {'imp': 0.0, 'shift': 0.0}, old+gate: {'imp': 0.008641975308641974, 'shift': 0.006790123456790123}, old no gate: {'imp': 0.012, 'shift': 0.011580246913580247}

Strong events, share reported abnormal by floor (smoothed), floors 1..10:

- 3x new: abnormal 0.89 0.94 0.94 0.95 0.94 0.95 0.95 0.95 0.95 0.95 | faulty 0.01 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00
- 3x old + gate: abnormal 0.00 0.01 0.03 0.07 0.11 0.11 0.13 0.14 0.13 0.13 | faulty 0.00 0.01 0.08 0.18 0.29 0.37 0.44 0.51 0.55 0.56
- 3x old, no gate: abnormal 0.00 0.01 0.03 0.07 0.11 0.11 0.13 0.14 0.13 0.13 | faulty 0.00 0.01 0.08 0.18 0.29 0.37 0.44 0.51 0.55 0.56
- 5x new: abnormal 0.96 0.96 0.96 0.96 0.96 0.96 0.96 0.96 0.96 0.96 | faulty 0.01 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00
- 5x old + gate: abnormal 0.00 0.02 0.07 0.10 0.12 0.11 0.11 0.11 0.11 0.11 | faulty 0.01 0.20 0.42 0.57 0.67 0.73 0.77 0.81 0.81 0.82
- 5x old, no gate: abnormal 0.00 0.02 0.07 0.10 0.12 0.11 0.11 0.11 0.11 0.11 | faulty 0.01 0.20 0.42 0.57 0.67 0.73 0.77 0.81 0.81 0.82

2x level and 30% stiffness loss (class mix [normal, abnormal, faulty], smoothed, all floors):

- new: 2x [0.2643333333333333, 0.7355111111111111, 0.00015555555555555556], stiffness loss [0.9505333333333333, 0.049466666666666666, 0.0]
- old + gate: 2x [0.8327777777777777, 0.05435555555555555, 0.11286666666666667], stiffness loss [0.9935111111111111, 0.0048222222222222225, 0.0016666666666666668]
- old, no gate: 2x [0.8327777777777777, 0.05435555555555555, 0.11286666666666667], stiffness loss [0.9919555555555556, 0.0048222222222222225, 0.0032222222222222222]

## az-only variant of the chosen configuration (reported only)

features ['az_std', 'az_kurt', 'az_domfreq', 'az_std_rel'], dev CV mean 0.9141, held-out {'false_alarm_smoothed': 0.051100000000000007, 'false_alarm_raw': 0.06365555555555556, 'detection_smoothed': 0.8787081339712919, 'detection_raw': 0.8942583732057416, 'detection_smoothed_impact': 0.8914507772020726, 'detection_smoothed_shift': 0.8677777777777778}

## dataset.csv (6000 isolated-recording windows)

```
{
 "windows": 6000,
 "old_ungated": {
  "accuracy": 0.9523333333333334,
  "confusion_true_rows_pred_cols": [
   [
    1996,
    4,
    0
   ],
   [
    90,
    1910,
    0
   ],
   [
    152,
    40,
    1808
   ]
  ]
 },
 "old_gated": {
  "accuracy": 0.9523333333333334,
  "confusion_true_rows_pred_cols": [
   [
    1996,
    4,
    0
   ],
   [
    90,
    1910,
    0
   ],
   [
    152,
    40,
    1808
   ]
  ]
 },
 "new": "not computed: the selected tree uses commissioning-relative features, which dataset.csv (isolated recordings) does not have"
}
```

## What the model needs on the device

- features used: ['az_std', 'az_kurt', 'az_domfreq', 'az_std_rel']
- outside the current five: ['az_std_rel']
- commissioning value needed: True (per node: median az_std (and gx_std) over a separate healthy recording of >= 20 windows, stored on the device (2 floats))
- tree size: 15 nodes, 150 B

## Note

RF configurations whose full-development fit exceeds 5120 B were not cross-validated (cannot be eligible by the rule); they are listed with their footprint.
