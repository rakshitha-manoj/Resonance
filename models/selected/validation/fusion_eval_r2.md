# R2: master fusion evaluation (AMBIENT / LOCAL / BUILDING)

Simulator only (building_sim: uniform shear building, stationary band-limited excitation, gx modelled). Baselines assume quiet commissioning. An event on an uninstrumented floor is a reported-only case. Selection on the development block, primary pair (floors 5, 10), test block scored once.

- seeds: {'dev': {'start': 5100000, 'n': 160}, 'test': {'start': 5200000, 'n': 160}} (all above 5,000,000, disjoint from earlier blocks, asserted)
- bootstrap: 2000 resamples by building, 95% percentile intervals

## Development table (primary pair, development block)

Rule: maximise mean(LOCAL recall, BUILDING recall) subject to AMBIENT recall >= 0.95. 'raw' = F is raw==faulty OR state==faulty; 'smoothed' = state==faulty.

| config | AMBIENT recall | false events / h | LOCAL recall | BUILDING recall | mean | isolated-impact recall (n) | eligible |
|---|---|---|---|---|---|---|---|
| d, T=1.5, raw | 0.900 | 256.6 | 0.827 | 0.883 | 0.855 | 0.836 (122) | False |
| d, T=1.5, smoothed | 0.907 | 240.2 | 0.767 | 0.883 | 0.825 | 0.344 (122) | False |
| d, T=2, raw | 0.937 | 162.4 | 0.921 | 0.727 | 0.824 | 0.943 (122) | False |
| b, T=1.5, no tree | 0.889 | 285.8 | 0.710 | 0.883 | 0.796 | 0.385 (122) | False |
| d, T=2, smoothed | 0.946 | 139.6 | 0.857 | 0.727 | 0.792 | 0.377 (122) | False |
| c, T=1.5, raw | 0.912 | 225.0 | 0.635 | 0.883 | 0.759 | 0.352 (122) | False |
| c, T=1.5, smoothed | 0.913 | 224.8 | 0.626 | 0.883 | 0.754 | 0.287 (122) | False |
| b, T=2, no tree | 0.947 | 137.4 | 0.670 | 0.727 | 0.699 | 0.369 (122) | False |
| d, T=3, raw | 0.946 | 138.9 | 0.946 | 0.444 | 0.695 | 0.975 (122) | False |
| d, T=3, smoothed **(chosen)** | 0.955 | 114.6 | 0.881 | 0.444 | 0.663 | 0.402 (122) | True |
| a, T=1.5, raw | 0.946 | 137.9 | 0.925 | 0.387 | 0.656 | 0.943 (122) | False |
| a, T=2, raw | 0.946 | 137.9 | 0.925 | 0.387 | 0.656 | 0.943 (122) | False |
| a, T=3, raw | 0.946 | 137.9 | 0.925 | 0.387 | 0.656 | 0.943 (122) | False |
| c, T=2, raw | 0.962 | 98.5 | 0.550 | 0.727 | 0.639 | 0.311 (122) | True |
| c, T=2, smoothed | 0.962 | 98.5 | 0.546 | 0.727 | 0.636 | 0.279 (122) | True |
| a, T=1.5, smoothed | 0.956 | 113.7 | 0.873 | 0.307 | 0.590 | 0.393 (122) | True |
| a, T=2, smoothed | 0.956 | 113.7 | 0.873 | 0.307 | 0.590 | 0.393 (122) | True |
| a, T=3, smoothed | 0.956 | 113.7 | 0.873 | 0.307 | 0.590 | 0.393 (122) | True |
| b, T=3, no tree | 0.974 | 67.7 | 0.466 | 0.444 | 0.455 | 0.246 (122) | True |
| c, T=3, raw | 0.981 | 50.1 | 0.370 | 0.444 | 0.407 | 0.213 (122) | True |
| c, T=3, smoothed | 0.981 | 50.1 | 0.368 | 0.444 | 0.406 | 0.205 (122) | True |

Chosen: **d, T=3, smoothed** (constraint AMBIENT >= 0.95 met).

## Test block, primary pair (floors [5, 10]) - chosen configuration

- buildings 160, superframes scored 43200
- AMBIENT recall 0.951 [0.942, 0.958]; false events per hour 126.502 [109.235, 149.064]
- LOCAL recall (correct slave) 0.884 [0.871, 0.896]; any event 0.886 [0.874, 0.898]; wrong slave 0.002 [0.001, 0.004]; slave correct given LOCAL output 0.998 [0.996, 0.999]; called BUILDING 0.001 [0.000, 0.001]
- BUILDING recall 0.432 [0.417, 0.448]; called LOCAL 0.206 [0.181, 0.231]; called AMBIENT 0.362 [0.333, 0.390]

Confusion matrix (counts; rows = truth, columns = output):

| truth \ output | AMBIENT | LOCAL(1) | LOCAL(2) | BUILDING |
|---|---|---|---|---|
| AMBIENT | 10340 | 214 | 314 | 7 |
| LOCAL(1) | 874 | 8059 | 34 | 2 |
| LOCAL(2) | 1168 | 0 | 7781 | 7 |
| BUILDING | 5210 | 370 | 2598 | 6222 |

Row fractions with 95% intervals:

| truth \ output | AMBIENT | LOCAL(1) | LOCAL(2) | BUILDING |
|---|---|---|---|---|
| AMBIENT | 0.951 [0.942, 0.958] | 0.020 [0.017, 0.022] | 0.029 [0.023, 0.036] | 0.001 [0.000, 0.001] |
| LOCAL(1) | 0.097 [0.085, 0.112] | 0.899 [0.882, 0.912] | 0.004 [0.001, 0.007] | 0.000 [0.000, 0.001] |
| LOCAL(2) | 0.130 [0.110, 0.152] | 0.000 [0.000, 0.000] | 0.869 [0.847, 0.889] | 0.001 [0.000, 0.002] |
| BUILDING | 0.362 [0.333, 0.390] | 0.026 [0.017, 0.035] | 0.180 [0.159, 0.202] | 0.432 [0.417, 0.448] |

Splits:

| subset | LOCAL recall (correct slave) | any event | BUILDING recall | called LOCAL | AMBIENT recall |
|---|---|---|---|---|---|
| impact | 0.888 [0.877, 0.898] | 0.889 [0.878, 0.899] | - | - | 0.671 [0.632, 0.704] |
| shift | 0.880 [0.858, 0.901] | 0.884 [0.862, 0.905] | - | - | n/a [n/a, n/a] |
| 2x building-wide | - | - | 0.055 [0.042, 0.068] | 0.245 [0.203, 0.286] | - |
| 3x building-wide | - | - | 0.353 [0.321, 0.385] | 0.322 [0.284, 0.359] | - |
| 5x building-wide | - | - | 0.889 [0.875, 0.902] | 0.051 [0.039, 0.064] | - |

AMBIENT recall by source:

- healthy: 0.976 [0.959, 0.989], 61.607 [28.393, 106.607] false events/h
- noise: 1.000 [1.000, 1.000], 0.000 [0.000, 0.000] false events/h
- non_event_windows_of_event_recordings: 0.671 [0.632, 0.704], 847.059 [760.276, 946.881] false events/h

LOCAL recall per slave: slave1 0.899 [0.882, 0.912]; slave2 0.869 [0.847, 0.889]

Isolated-impact recall (impact windows with no neighbouring impact window; LOCAL(correct slave) required):

- smoothed_variant: isolated 0.434 [0.352, 0.514] (n=143); clustered 0.896 [0.886, 0.905] (n=8182)
- raw_variant: isolated 0.972 [0.942, 0.993] (n=143); clustered 0.966 [0.960, 0.971] (n=8182)

Degraded mode (one slave alive; only LOCAL(alive) or AMBIENT possible):

- only_slave1_alive: AMBIENT recall 0.978 [0.975, 0.981]; LOCAL recall 0.899 [0.883, 0.913]; BUILDING windows called LOCAL(alive) 0.325 [0.294, 0.356]
- only_slave2_alive: AMBIENT recall 0.970 [0.961, 0.976]; LOCAL recall 0.869 [0.847, 0.890]; BUILDING windows called LOCAL(alive) 0.532 [0.498, 0.564]

Uninstrumented-floor events (reported only; no truth defined; share of output):

- uimp event windows: AMBIENT 0.965 [0.945, 0.982], LOCAL(1) 0.002 [0.000, 0.004], LOCAL(2) 0.031 [0.016, 0.049], BUILDING 0.002 [0.000, 0.005]
- uimp non-event windows: AMBIENT 0.975 [0.952, 0.992], LOCAL(1) 0.002 [0.000, 0.006], LOCAL(2) 0.023 [0.008, 0.043], BUILDING 0.000 [0.000, 0.000]
- ushift event windows: AMBIENT 0.968 [0.951, 0.981], LOCAL(1) 0.002 [0.000, 0.004], LOCAL(2) 0.031 [0.018, 0.046], BUILDING 0.000 [0.000, 0.000]
- ushift non-event windows: AMBIENT 0.000 [0.000, 0.000], LOCAL(1) 0.000 [0.000, 0.000], LOCAL(2) 0.000 [0.000, 0.000], BUILDING 0.000 [0.000, 0.000]

## Test block, secondary pair (floors [3, 7]) - chosen configuration

- buildings 160, superframes scored 43200
- AMBIENT recall 0.957 [0.952, 0.962]; false events per hour 109.570 [96.706, 124.401]
- LOCAL recall (correct slave) 0.893 [0.880, 0.904]; any event 0.895 [0.882, 0.905]; wrong slave 0.001 [0.000, 0.003]; slave correct given LOCAL output 0.999 [0.997, 1.000]; called BUILDING 0.000 [0.000, 0.001]
- BUILDING recall 0.379 [0.362, 0.396]; called LOCAL 0.167 [0.145, 0.189]; called AMBIENT 0.453 [0.422, 0.485]

Confusion matrix (counts; rows = truth, columns = output):

| truth \ output | AMBIENT | LOCAL(1) | LOCAL(2) | BUILDING |
|---|---|---|---|---|
| AMBIENT | 10313 | 185 | 271 | 3 |
| LOCAL(1) | 875 | 8146 | 23 | 5 |
| LOCAL(2) | 1026 | 1 | 7950 | 2 |
| BUILDING | 6530 | 86 | 2321 | 5463 |

Row fractions with 95% intervals:

| truth \ output | AMBIENT | LOCAL(1) | LOCAL(2) | BUILDING |
|---|---|---|---|---|
| AMBIENT | 0.957 [0.952, 0.962] | 0.017 [0.015, 0.020] | 0.025 [0.021, 0.030] | 0.000 [0.000, 0.001] |
| LOCAL(1) | 0.097 [0.083, 0.113] | 0.900 [0.883, 0.915] | 0.003 [0.001, 0.005] | 0.001 [0.000, 0.001] |
| LOCAL(2) | 0.114 [0.098, 0.131] | 0.000 [0.000, 0.000] | 0.885 [0.869, 0.902] | 0.000 [0.000, 0.001] |
| BUILDING | 0.453 [0.422, 0.485] | 0.006 [0.003, 0.010] | 0.161 [0.140, 0.183] | 0.379 [0.362, 0.396] |

Splits:

| subset | LOCAL recall (correct slave) | any event | BUILDING recall | called LOCAL | AMBIENT recall |
|---|---|---|---|---|---|
| impact | 0.883 [0.872, 0.894] | 0.885 [0.874, 0.895] | - | - | 0.652 [0.615, 0.685] |
| shift | 0.901 [0.879, 0.922] | 0.903 [0.881, 0.923] | - | - | n/a [n/a, n/a] |
| 2x building-wide | - | - | 0.030 [0.021, 0.040] | 0.155 [0.121, 0.191] | - |
| 3x building-wide | - | - | 0.268 [0.238, 0.299] | 0.285 [0.246, 0.325] | - |
| 5x building-wide | - | - | 0.840 [0.816, 0.862] | 0.062 [0.048, 0.076] | - |

AMBIENT recall by source:

- healthy: 0.989 [0.980, 0.996], 27.321 [9.107, 51.429] false events/h
- noise: 1.000 [1.000, 1.000], 0.000 [0.000, 0.000] false events/h
- non_event_windows_of_event_recordings: 0.652 [0.615, 0.685], 895.173 [808.894, 989.220] false events/h

LOCAL recall per slave: slave1 0.900 [0.883, 0.915]; slave2 0.885 [0.869, 0.902]

Isolated-impact recall (impact windows with no neighbouring impact window; LOCAL(correct slave) required):

- smoothed_variant: isolated 0.397 [0.307, 0.496] (n=126); clustered 0.890 [0.880, 0.900] (n=8302)
- raw_variant: isolated 0.952 [0.915, 0.985] (n=126); clustered 0.961 [0.955, 0.967] (n=8302)

Degraded mode (one slave alive; only LOCAL(alive) or AMBIENT possible):

- only_slave1_alive: AMBIENT recall 0.981 [0.979, 0.984]; LOCAL recall 0.901 [0.884, 0.915]; BUILDING windows called LOCAL(alive) 0.160 [0.137, 0.183]
- only_slave2_alive: AMBIENT recall 0.974 [0.969, 0.979]; LOCAL recall 0.886 [0.869, 0.902]; BUILDING windows called LOCAL(alive) 0.443 [0.408, 0.480]

Uninstrumented-floor events (reported only; no truth defined; share of output):

- uimp event windows: AMBIENT 0.983 [0.970, 0.994], LOCAL(1) 0.000 [0.000, 0.000], LOCAL(2) 0.015 [0.005, 0.028], BUILDING 0.001 [0.000, 0.003]
- uimp non-event windows: AMBIENT 0.988 [0.976, 0.997], LOCAL(1) 0.000 [0.000, 0.000], LOCAL(2) 0.012 [0.003, 0.024], BUILDING 0.000 [0.000, 0.000]
- ushift event windows: AMBIENT 0.987 [0.979, 0.994], LOCAL(1) 0.000 [0.000, 0.000], LOCAL(2) 0.013 [0.006, 0.021], BUILDING 0.000 [0.000, 0.000]
- ushift non-event windows: AMBIENT 0.000 [0.000, 0.000], LOCAL(1) 0.000 [0.000, 0.000], LOCAL(2) 0.000 [0.000, 0.000], BUILDING 0.000 [0.000, 0.000]

## Reference check

Scalar RefFusion vs the vectorised rules: 11340 superframes, 0 mismatches.

