# R3: one standalone node vs the 3-ESP32 hierarchy

## Plain-language summary

**What was compared.** The same R2b test recordings (simulated buildings, 28 h of healthy monitoring plus nine event kinds) scored for: one node at floor 10 (S1 old tree, S3 ratio rule), one node at floor 5 (S2 old tree), and the 3-ESP32 hierarchy H (slaves on floors 5 and 10). Simulation only; every system sees the same sensor signals. Intervals are in the tables below.

**False alarms (onsets per hour of healthy ambient monitoring; the combined rate with noise-only hours is in the tables).** S1 39.0, S2 11.7, S3 2.41, H 2.54. The old tree used alone is far noisier than the ratio rule. S3 and H are statistically indistinguishable (2.41 vs 2.54, intervals overlap): the hierarchy does NOT reduce false alarms relative to one ratio node, and adds none measurable.

**Detecting that something happened (any alarm counts).** Mean of LOCAL and BUILDING event detection: S1 0.712, S2 0.570, S3 0.530, H 0.651. S1 detects the most events of every kind, but at 15 times H's false-alarm rate, so the two are not comparable at one operating point. Against S3, which has the same false-alarm rate as H: building-wide events S3 0.833 vs H 0.844; events on S3's own floor (10) S3 0.429 vs H 0.429; events on the other floor (5) S3 0.022 vs H 0.487. So for building-wide events and for events on its own floor, one ratio node is just as good as the hierarchy; the hierarchy's extra detection is the second floor.

**What the hierarchy adds.** (1) Event type and floor: H says LOCAL(slave) or BUILDING (correct-type recall LOCAL 0.453, BUILDING 0.811); a single node cannot, so single nodes score 0 on correct type BY CONSTRUCTION. (2) Two-floor coverage (previous paragraph). (3) Dead-node handling: when a slave dies the master declares it DEAD after about 4.2 s in the simulator, H keeps working in DEGRADED mode on the other slave (any-alarm detection of LOCAL events 0.226 / 0.250 with slave 1 / slave 2 dead, building-wide events still alarm as LOCAL(alive) 0.833 / 0.822, but never typed BUILDING); a single node that dies is blind and silent unless an external watchdog exists. The master writes a dedicated `node` DEAD/ALIVE line to the JSONL log when it declares a slave dead or alive again (see the visibility table).

**What it does not add.** Sensitivity to weak or rare events: sparse single impacts and 2x building-wide events are missed by the ratio rule whether it runs in one node or in H, and S3 and H have the same false-alarm rate. H needs three boards, a working radio link and about 0.36 MB/h of radio traffic; one node needs none of that. Packet loss: with 20% i.i.d. loss BUILDING correct-type recall falls 0.811 to 0.681 and LOCAL 0.453 to 0.413; false DEAD declarations of healthy slaves are 0.12 per slave-hour at 10% and 4.8 at 20%. Where only 'did something happen on my floor or building-wide' matters, a single ratio node (S3) is as good and simpler.

## Limits (read before using any number)

- Simulation only (`building_sim`): uniform shear building, two instrumented floors, stationary band-limited excitation, i.i.d. packet loss (real loss is bursty), no real hardware, no ESP-NOW airtime or collisions.
- R2b TEST block only (seeds 6,200,000+), scoring only; nothing was selected on it. H (rule b, T=3.0, k=1) was fixed in R2b on the development block. The single-node configurations are the project's existing tree/gate/smoothing and the same ratio threshold 3.0, not tuned here.
- Baselines are medians of 20 quiet commissioning windows; real drift in ambient level moves every ratio rule (S3 and H alike).
- 'Detected' = at least one alarm of the stated kind anywhere in the event recording (30 windows), as in R2b. In-span variants (event windows + 3 superframes) are in the JSON.
- Bootstrap 95% intervals resample buildings (2000 resamples).
- The packet-loss, data-volume and fault-visibility sections use the first 240 of the test buildings (seeds 6200000-6200239; raw windows regenerated and verified identical to the R2b cache) because netsim needs raw windows; all Python-side tables use every building of the test block.

## Main comparison, primary pair (floors 5/10), 240 test buildings, 28.0 h of healthy + noise-only monitoring

Single nodes: S1 and S3 on floor 10, S2 on floor 5. Hierarchy H: slaves on 5 and 10.

### False onsets per hour

Main figure = healthy ambient only (noise-only recordings cannot produce onsets, so the combined rate is lower by the hour ratio 240/300).

| config | healthy ambient (main) | all monitoring (healthy + noise-only) | noise-only |
|---|---|---|---|
| S1 (tree, hi floor) | 39.02 [31.02, 47.68] | 31.21 [24.82, 38.14] | 0.00 [0.00, 0.00] |
| S2 (tree, lo floor) | 11.65 [7.23, 16.38] | 9.32 [5.79, 13.11] | 0.00 [0.00, 0.00] |
| S3 (ratio, hi floor) | 2.41 [1.25, 3.93] | 1.93 [1.00, 3.14] | 0.00 [0.00, 0.00] |
| H (hierarchy) | 2.54 [1.29, 4.06] | 2.04 [1.04, 3.25] | 0.00 [0.00, 0.00] |

### Event detection, any alarm counts

| config | LOCAL events (mean) | dense impacts | sparse impact | frequency shift | BUILDING events (mean) | 2x | 3x | 5x |
|---|---|---|---|---|---|---|---|---|
| S1 (tree, hi floor) | 0.501 [0.477, 0.525] | 0.625 [0.598, 0.652] | 0.263 [0.221, 0.308] | 0.617 [0.592, 0.642] | 0.922 [0.901, 0.942] | 0.800 [0.750, 0.850] | 0.967 [0.942, 0.988] | 1.000 [1.000, 1.000] |
| S2 (tree, lo floor) | 0.391 [0.376, 0.406] | 0.527 [0.512, 0.542] | 0.117 [0.087, 0.148] | 0.529 [0.515, 0.546] | 0.750 [0.718, 0.783] | 0.454 [0.396, 0.513] | 0.817 [0.767, 0.867] | 0.979 [0.958, 0.996] |
| S3 (ratio, hi floor) | 0.226 [0.213, 0.240] | 0.510 [0.496, 0.525] | 0.035 [0.019, 0.054] | 0.131 [0.104, 0.158] | 0.833 [0.812, 0.854] | 0.500 [0.438, 0.562] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| H (hierarchy) | 0.458 [0.441, 0.477] | 0.990 [0.979, 0.998] | 0.062 [0.042, 0.085] | 0.323 [0.277, 0.369] | 0.844 [0.824, 0.865] | 0.533 [0.471, 0.596] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |

### LOCAL events split by the floor the event is on (any alarm)

A single node sees its own floor directly and the other floor only through the building response. Lo floor = 5, hi floor = 10.

| config | events on lo floor | dense | sparse | shift | events on hi floor | dense | sparse | shift |
|---|---|---|---|---|---|---|---|---|
| S1 (tree, hi floor) | 0.235 [0.197, 0.272] | 0.250 [0.196, 0.304] | 0.221 [0.171, 0.271] | 0.233 [0.183, 0.283] | 0.768 [0.749, 0.787] | 1.000 [1.000, 1.000] | 0.304 [0.246, 0.362] | 1.000 [1.000, 1.000] |
| S2 (tree, lo floor) | 0.719 [0.704, 0.736] | 1.000 [1.000, 1.000] | 0.163 [0.117, 0.212] | 0.996 [0.988, 1.000] | 0.062 [0.042, 0.085] | 0.054 [0.025, 0.083] | 0.071 [0.042, 0.104] | 0.062 [0.033, 0.096] |
| S3 (ratio, hi floor) | 0.022 [0.011, 0.035] | 0.037 [0.017, 0.062] | 0.017 [0.004, 0.033] | 0.013 [0.000, 0.025] | 0.429 [0.407, 0.451] | 0.983 [0.967, 0.996] | 0.054 [0.029, 0.083] | 0.250 [0.196, 0.304] |
| H (hierarchy) | 0.487 [0.463, 0.512] | 0.996 [0.988, 1.000] | 0.071 [0.042, 0.104] | 0.396 [0.333, 0.458] | 0.429 [0.407, 0.451] | 0.983 [0.967, 0.996] | 0.054 [0.029, 0.083] | 0.250 [0.196, 0.304] |

### Correct event type (LOCAL with the right slave, or BUILDING, output for at least one superframe)

Single nodes cannot tell local from building-wide or name a floor: their 0 is BY CONSTRUCTION, not a measurement.

| config | LOCAL (right slave) | dense | sparse | shift | BUILDING | 2x | 3x | 5x |
|---|---|---|---|---|---|---|---|---|
| S1 (tree, hi floor) | 0 (by construction) | 0 | 0 | 0 | 0 (by construction) | 0 | 0 | 0 |
| S2 (tree, lo floor) | 0 (by construction) | 0 | 0 | 0 | 0 (by construction) | 0 | 0 | 0 |
| S3 (ratio, hi floor) | 0 (by construction) | 0 | 0 | 0 | 0 (by construction) | 0 | 0 | 0 |
| H (hierarchy) | 0.453 [0.436, 0.471] | 0.990 [0.979, 0.998] | 0.048 [0.031, 0.069] | 0.321 [0.275, 0.365] | 0.811 [0.789, 0.832] | 0.442 [0.379, 0.504] | 0.992 [0.979, 1.000] | 1.000 [1.000, 1.000] |

### Behaviour with a dead node (lossless network, Python reference)

A dead single node is blind: it detects nothing, raises no alarm and, without an external watchdog, nothing tells the user it is dead. H with one dead slave reports DEGRADED and continues with the other slave (LOCAL(alive) or AMBIENT only; BUILDING cannot be output). Events on the dead slave's floor reach the alive slave only through the building response.

| case | false onsets/h | any alarm, LOCAL events | LOCAL on alive slave's floor | LOCAL on dead slave's floor | any alarm, BUILDING events | correct type: LOCAL on alive floor | correct type: BUILDING |
|---|---|---|---|---|---|---|---|
| single node dead (S1, S2, S3) | 0 (no data) | 0 | 0 | 0 | 0 | 0 | 0 |
| H, slave 1 dead (DEGRADED) | 1.93 [1.00, 3.14] | 0.226 [0.213, 0.240] | 0.429 [0.407, 0.451] | 0.022 [0.011, 0.035] | 0.833 [0.812, 0.854] | 0.429 [0.407, 0.451] | 0.000 [0.000, 0.000] |
| H, slave 2 dead (DEGRADED) | 1.39 [0.57, 2.43] | 0.250 [0.237, 0.263] | 0.487 [0.463, 0.512] | 0.013 [0.006, 0.021] | 0.822 [0.800, 0.843] | 0.487 [0.463, 0.512] | 0.000 [0.000, 0.000] |

### Degraded mode in detail (rule b, T=3, k=1: LOCAL only on the alive slave), compared with a single node on the alive floor

False onsets are per hour of healthy ambient monitoring (combined rate in the table above). With one slave BUILDING can never be output, so a building-wide event appears as LOCAL(alive): the last three columns are the share of building-wide events that raise that (wrong-type) LOCAL alarm.

| case | false onsets/h | LOCAL recall on alive floor | dense | sparse | shift | building-wide shows as LOCAL(alive): 2x | 3x | 5x |
|---|---|---|---|---|---|---|---|---|
| H, slave 1 dead, alive floor 10 | 2.41 [1.25, 3.93] | 0.429 [0.407, 0.451] | 0.983 [0.967, 0.996] | 0.054 [0.029, 0.083] | 0.250 [0.196, 0.304] | 0.500 [0.438, 0.562] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| single node S1 on floor 10 (any alarm) | 39.02 [31.02, 47.68] | 0.768 [0.749, 0.787] | 1.000 [1.000, 1.000] | 0.304 [0.246, 0.362] | 1.000 [1.000, 1.000] | 0.800 [0.750, 0.850] | 0.967 [0.942, 0.988] | 1.000 [1.000, 1.000] |
| single node S3 on floor 10 (any alarm) | 2.41 [1.25, 3.93] | 0.429 [0.407, 0.451] | 0.983 [0.967, 0.996] | 0.054 [0.029, 0.083] | 0.250 [0.196, 0.304] | 0.500 [0.438, 0.562] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| H, slave 2 dead, alive floor 5 | 1.74 [0.71, 3.04] | 0.487 [0.463, 0.512] | 0.996 [0.988, 1.000] | 0.071 [0.042, 0.104] | 0.396 [0.333, 0.458] | 0.475 [0.408, 0.537] | 0.992 [0.979, 1.000] | 1.000 [1.000, 1.000] |
| single node S2 on floor 5 (any alarm) | 11.65 [7.23, 16.38] | 0.719 [0.704, 0.736] | 1.000 [1.000, 1.000] | 0.163 [0.117, 0.212] | 0.996 [0.988, 1.000] | 0.454 [0.396, 0.513] | 0.817 [0.767, 0.867] | 0.979 [0.958, 0.996] |
| single node S3lo on floor 5 (any alarm) | 1.74 [0.71, 3.04] | 0.487 [0.463, 0.512] | 0.996 [0.988, 1.000] | 0.071 [0.042, 0.104] | 0.396 [0.333, 0.458] | 0.475 [0.408, 0.537] | 0.992 [0.979, 1.000] | 1.000 [1.000, 1.000] |

Reading this: degraded H with rule b is the same detector as the single ratio node on the alive floor (so S3 / S3lo match it), minus the type information that needs two slaves. What it adds over a lone node is that it was commissioned in a pair and the master KNOWS the other slave is gone.

### POST-HOC context: rule c, T=3, k=1 (the R2b runner-up under the fixed criterion; NOT a selection, the final configuration stays rule b)

Rule c: both e = BUILDING; F (tree faulty) and e on one slave with the other's ratio below 1.5 = LOCAL(that slave). Wrong-class = share of events of that kind during which some event class other than the true one was output (BUILDING called LOCAL, LOCAL called BUILDING, or wrong slave).

| config | false onsets/h healthy (main) | combined | LOCAL correct-type | BUILDING correct-type | 2x | 3x | 5x | wrong-class 2x | wrong-class 3x | wrong-class 5x | wrong-class LOCAL events |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rule b (final) | 2.54 [1.29, 4.06] | 2.04 [1.04, 3.25] | 0.453 [0.436, 0.471] | 0.811 [0.789, 0.832] | 0.442 [0.379, 0.504] | 0.992 [0.979, 1.000] | 1.000 [1.000, 1.000] | 0.229 [0.175, 0.283] | 0.592 [0.529, 0.658] | 0.283 [0.229, 0.338] | 0.017 [0.010, 0.024] |
| rule c (post-hoc context) | 1.70 [0.67, 2.99] | 1.36 [0.54, 2.39] | 0.433 [0.417, 0.449] | 0.811 [0.789, 0.832] | 0.442 [0.379, 0.504] | 0.992 [0.979, 1.000] | 1.000 [1.000, 1.000] | 0.021 [0.004, 0.042] | 0.033 [0.013, 0.058] | 0.004 [0.000, 0.013] | 0.017 [0.010, 0.024] |

## Main comparison, secondary pair (floors 3/7), 240 test buildings, 28.0 h of healthy + noise-only monitoring

Single nodes: S1 and S3 on floor 7, S2 on floor 3. Hierarchy H: slaves on 3 and 7.

### False onsets per hour

Main figure = healthy ambient only (noise-only recordings cannot produce onsets, so the combined rate is lower by the hour ratio 240/300).

| config | healthy ambient (main) | all monitoring (healthy + noise-only) | noise-only |
|---|---|---|---|
| S1 (tree, hi floor) | 27.32 [20.36, 35.09] | 21.86 [16.29, 28.07] | 0.00 [0.00, 0.00] |
| S2 (tree, lo floor) | 0.98 [0.36, 1.83] | 0.79 [0.29, 1.46] | 0.00 [0.00, 0.00] |
| S3 (ratio, hi floor) | 2.23 [0.98, 3.84] | 1.79 [0.79, 3.07] | 0.00 [0.00, 0.00] |
| H (hierarchy) | 2.28 [0.98, 3.88] | 1.82 [0.79, 3.11] | 0.00 [0.00, 0.00] |

### Event detection, any alarm counts

| config | LOCAL events (mean) | dense impacts | sparse impact | frequency shift | BUILDING events (mean) | 2x | 3x | 5x |
|---|---|---|---|---|---|---|---|---|
| S1 (tree, hi floor) | 0.462 [0.441, 0.484] | 0.567 [0.546, 0.590] | 0.221 [0.181, 0.260] | 0.598 [0.575, 0.623] | 0.875 [0.850, 0.900] | 0.704 [0.650, 0.762] | 0.921 [0.887, 0.954] | 1.000 [1.000, 1.000] |
| S2 (tree, lo floor) | 0.355 [0.348, 0.362] | 0.502 [0.500, 0.506] | 0.062 [0.042, 0.083] | 0.500 [0.500, 0.500] | 0.460 [0.424, 0.496] | 0.129 [0.087, 0.171] | 0.400 [0.342, 0.463] | 0.850 [0.804, 0.892] |
| S3 (ratio, hi floor) | 0.224 [0.210, 0.237] | 0.496 [0.485, 0.504] | 0.050 [0.031, 0.071] | 0.125 [0.098, 0.152] | 0.829 [0.808, 0.850] | 0.492 [0.429, 0.554] | 0.996 [0.988, 1.000] | 1.000 [1.000, 1.000] |
| H (hierarchy) | 0.508 [0.488, 0.526] | 0.992 [0.983, 0.998] | 0.092 [0.067, 0.119] | 0.440 [0.396, 0.483] | 0.831 [0.810, 0.851] | 0.496 [0.433, 0.558] | 0.996 [0.988, 1.000] | 1.000 [1.000, 1.000] |

### LOCAL events split by the floor the event is on (any alarm)

A single node sees its own floor directly and the other floor only through the building response. Lo floor = 3, hi floor = 7.

| config | events on lo floor | dense | sparse | shift | events on hi floor | dense | sparse | shift |
|---|---|---|---|---|---|---|---|---|
| S1 (tree, hi floor) | 0.174 [0.140, 0.208] | 0.133 [0.092, 0.179] | 0.192 [0.146, 0.242] | 0.196 [0.150, 0.246] | 0.750 [0.733, 0.768] | 1.000 [1.000, 1.000] | 0.250 [0.200, 0.304] | 1.000 [1.000, 1.000] |
| S2 (tree, lo floor) | 0.707 [0.694, 0.721] | 1.000 [1.000, 1.000] | 0.121 [0.083, 0.163] | 1.000 [1.000, 1.000] | 0.003 [0.000, 0.007] | 0.004 [0.000, 0.013] | 0.004 [0.000, 0.013] | 0.000 [0.000, 0.000] |
| S3 (ratio, hi floor) | 0.015 [0.007, 0.026] | 0.008 [0.000, 0.021] | 0.033 [0.013, 0.058] | 0.004 [0.000, 0.013] | 0.432 [0.408, 0.456] | 0.983 [0.967, 0.996] | 0.067 [0.037, 0.100] | 0.246 [0.192, 0.300] |
| H (hierarchy) | 0.583 [0.558, 0.610] | 1.000 [1.000, 1.000] | 0.117 [0.079, 0.158] | 0.633 [0.575, 0.692] | 0.432 [0.408, 0.456] | 0.983 [0.967, 0.996] | 0.067 [0.037, 0.100] | 0.246 [0.192, 0.300] |

### Correct event type (LOCAL with the right slave, or BUILDING, output for at least one superframe)

Single nodes cannot tell local from building-wide or name a floor: their 0 is BY CONSTRUCTION, not a measurement.

| config | LOCAL (right slave) | dense | sparse | shift | BUILDING | 2x | 3x | 5x |
|---|---|---|---|---|---|---|---|---|
| S1 (tree, hi floor) | 0 (by construction) | 0 | 0 | 0 | 0 (by construction) | 0 | 0 | 0 |
| S2 (tree, lo floor) | 0 (by construction) | 0 | 0 | 0 | 0 (by construction) | 0 | 0 | 0 |
| S3 (ratio, hi floor) | 0 (by construction) | 0 | 0 | 0 | 0 (by construction) | 0 | 0 | 0 |
| H (hierarchy) | 0.501 [0.483, 0.519] | 0.992 [0.983, 0.998] | 0.073 [0.050, 0.096] | 0.440 [0.396, 0.483] | 0.733 [0.710, 0.757] | 0.283 [0.225, 0.338] | 0.917 [0.879, 0.950] | 1.000 [1.000, 1.000] |

### Behaviour with a dead node (lossless network, Python reference)

A dead single node is blind: it detects nothing, raises no alarm and, without an external watchdog, nothing tells the user it is dead. H with one dead slave reports DEGRADED and continues with the other slave (LOCAL(alive) or AMBIENT only; BUILDING cannot be output). Events on the dead slave's floor reach the alive slave only through the building response.

| case | false onsets/h | any alarm, LOCAL events | LOCAL on alive slave's floor | LOCAL on dead slave's floor | any alarm, BUILDING events | correct type: LOCAL on alive floor | correct type: BUILDING |
|---|---|---|---|---|---|---|---|
| single node dead (S1, S2, S3) | 0 (no data) | 0 | 0 | 0 | 0 | 0 | 0 |
| H, slave 1 dead (DEGRADED) | 1.79 [0.79, 3.07] | 0.224 [0.210, 0.237] | 0.432 [0.408, 0.456] | 0.015 [0.007, 0.026] | 0.829 [0.808, 0.850] | 0.432 [0.408, 0.456] | 0.000 [0.000, 0.000] |
| H, slave 2 dead (DEGRADED) | 0.86 [0.25, 1.68] | 0.293 [0.281, 0.306] | 0.582 [0.557, 0.608] | 0.004 [0.000, 0.010] | 0.735 [0.711, 0.758] | 0.582 [0.557, 0.608] | 0.000 [0.000, 0.000] |

### Degraded mode in detail (rule b, T=3, k=1: LOCAL only on the alive slave), compared with a single node on the alive floor

False onsets are per hour of healthy ambient monitoring (combined rate in the table above). With one slave BUILDING can never be output, so a building-wide event appears as LOCAL(alive): the last three columns are the share of building-wide events that raise that (wrong-type) LOCAL alarm.

| case | false onsets/h | LOCAL recall on alive floor | dense | sparse | shift | building-wide shows as LOCAL(alive): 2x | 3x | 5x |
|---|---|---|---|---|---|---|---|---|
| H, slave 1 dead, alive floor 7 | 2.23 [0.98, 3.84] | 0.432 [0.408, 0.456] | 0.983 [0.967, 0.996] | 0.067 [0.037, 0.100] | 0.246 [0.192, 0.300] | 0.492 [0.429, 0.554] | 0.996 [0.988, 1.000] | 1.000 [1.000, 1.000] |
| single node S1 on floor 7 (any alarm) | 27.32 [20.36, 35.09] | 0.750 [0.733, 0.768] | 1.000 [1.000, 1.000] | 0.250 [0.200, 0.304] | 1.000 [1.000, 1.000] | 0.704 [0.650, 0.762] | 0.921 [0.887, 0.954] | 1.000 [1.000, 1.000] |
| single node S3 on floor 7 (any alarm) | 2.23 [0.98, 3.84] | 0.432 [0.408, 0.456] | 0.983 [0.967, 0.996] | 0.067 [0.037, 0.100] | 0.246 [0.192, 0.300] | 0.492 [0.429, 0.554] | 0.996 [0.988, 1.000] | 1.000 [1.000, 1.000] |
| H, slave 2 dead, alive floor 3 | 1.07 [0.31, 2.10] | 0.582 [0.557, 0.608] | 1.000 [1.000, 1.000] | 0.113 [0.075, 0.154] | 0.633 [0.575, 0.692] | 0.287 [0.229, 0.346] | 0.917 [0.879, 0.950] | 1.000 [1.000, 1.000] |
| single node S2 on floor 3 (any alarm) | 0.98 [0.36, 1.83] | 0.707 [0.694, 0.721] | 1.000 [1.000, 1.000] | 0.121 [0.083, 0.163] | 1.000 [1.000, 1.000] | 0.129 [0.087, 0.171] | 0.400 [0.342, 0.463] | 0.850 [0.804, 0.892] |
| single node S3lo on floor 3 (any alarm) | 1.07 [0.31, 2.10] | 0.582 [0.557, 0.608] | 1.000 [1.000, 1.000] | 0.113 [0.075, 0.154] | 0.633 [0.575, 0.692] | 0.287 [0.229, 0.346] | 0.917 [0.879, 0.950] | 1.000 [1.000, 1.000] |

Reading this: degraded H with rule b is the same detector as the single ratio node on the alive floor (so S3 / S3lo match it), minus the type information that needs two slaves. What it adds over a lone node is that it was commissioned in a pair and the master KNOWS the other slave is gone.

### POST-HOC context: rule c, T=3, k=1 (the R2b runner-up under the fixed criterion; NOT a selection, the final configuration stays rule b)

Rule c: both e = BUILDING; F (tree faulty) and e on one slave with the other's ratio below 1.5 = LOCAL(that slave). Wrong-class = share of events of that kind during which some event class other than the true one was output (BUILDING called LOCAL, LOCAL called BUILDING, or wrong slave).

| config | false onsets/h healthy (main) | combined | LOCAL correct-type | BUILDING correct-type | 2x | 3x | 5x | wrong-class 2x | wrong-class 3x | wrong-class 5x | wrong-class LOCAL events |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rule b (final) | 2.28 [0.98, 3.88] | 1.82 [0.79, 3.11] | 0.501 [0.483, 0.519] | 0.733 [0.710, 0.757] | 0.283 [0.225, 0.338] | 0.917 [0.879, 0.950] | 1.000 [1.000, 1.000] | 0.358 [0.300, 0.421] | 0.804 [0.754, 0.854] | 0.467 [0.404, 0.529] | 0.010 [0.005, 0.015] |
| rule c (post-hoc context) | 1.07 [0.31, 2.10] | 0.86 [0.25, 1.68] | 0.478 [0.462, 0.495] | 0.733 [0.710, 0.757] | 0.283 [0.225, 0.338] | 0.917 [0.879, 0.950] | 1.000 [1.000, 1.000] | 0.025 [0.008, 0.046] | 0.037 [0.017, 0.062] | 0.008 [0.000, 0.021] | 0.008 [0.004, 0.013] |

## Data sent

| channel | per second | per hour |
|---|---|---|
| raw streaming, 6 axes x 2 B x 100 Hz, per node | 1200 B | 4.32 MB |
| raw streaming, the 2 axes the model uses, per node | 400 B | 1.44 MB |
| hierarchy radio, master (beacons + polls), lossless | 37.9 B | 136 kB |
| hierarchy radio, slave 1 (reports) | 31.4 B | 113 kB |
| hierarchy radio, slave 2 (reports) | 31.4 B | 113 kB |
| hierarchy radio, all three nodes | 100.7 B | 363 kB |
| events-only uplink to the cloud (JSONL, healthy monitoring, lossless) | 2.92 B | 10.5 kB (47.9 lines/h: 43.4 periodic status + 4.5 begin/change) |

Hierarchy bytes are measured by netsim on the test traces (application payload only; ESP-NOW/802.11 framing and acknowledgements excluded). Raw streaming of the two slave nodes would be 8.64 MB/h (6 axes) or 2.88 MB/h (2 axes), about 8x the whole hierarchy radio. A single node that classifies on the device and uplinks only its alarms would send even less than the hierarchy; the hierarchy's radio traffic is the price of the local/building-wide decision.

## Network loss effect on H (240 test buildings, primary pair, netsim with fusion.h on traces built from the R2b test recordings)

i.i.d. loss per link on every packet (beacon, poll, report). Seeds per level: 0%: 1, 5%: 3, 10%: 3, 20%: 3. Intervals resample buildings. 'Correct type' columns are the fusion event recall of R2b; the last column is any-alarm detection.

| loss | false onsets/h (healthy, main / combined) | LOCAL correct-type | dense | sparse | shift | BUILDING correct-type | 2x | 3x | 5x | any alarm (mean of LOCAL, BUILDING) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0% | 2.54 [1.29, 4.06] / 2.04 [1.04, 3.25] | 0.453 [0.436, 0.471] | 0.990 [0.979, 0.998] | 0.048 [0.031, 0.069] | 0.321 [0.275, 0.365] | 0.811 [0.789, 0.832] | 0.442 [0.379, 0.504] | 0.992 [0.979, 1.000] | 1.000 [1.000, 1.000] | 0.651 [0.636, 0.667] |
| 5% | 2.51 [1.26, 4.05] / 2.01 [1.01, 3.24] | 0.449 [0.432, 0.467] | 0.988 [0.978, 0.997] | 0.047 [0.030, 0.067] | 0.312 [0.267, 0.356] | 0.800 [0.778, 0.821] | 0.418 [0.357, 0.479] | 0.982 [0.965, 0.994] | 1.000 [1.000, 1.000] | 0.647 [0.632, 0.662] |
| 10% | 2.31 [1.22, 3.65] / 1.85 [0.98, 2.92] | 0.442 [0.425, 0.459] | 0.983 [0.972, 0.992] | 0.046 [0.030, 0.065] | 0.297 [0.253, 0.341] | 0.778 [0.757, 0.799] | 0.365 [0.308, 0.422] | 0.969 [0.950, 0.986] | 1.000 [1.000, 1.000] | 0.637 [0.622, 0.652] |
| 20% | 2.16 [1.04, 3.62] / 1.73 [0.83, 2.89] | 0.413 [0.397, 0.428] | 0.968 [0.954, 0.980] | 0.035 [0.022, 0.051] | 0.235 [0.196, 0.274] | 0.681 [0.658, 0.702] | 0.208 [0.164, 0.250] | 0.835 [0.797, 0.871] | 1.000 [1.000, 1.000] | 0.601 [0.586, 0.614] |

Hierarchy radio bytes per hour by loss level (retries add polls):

| loss | master | slave 1 | slave 2 |
|---|---|---|---|
| 0% | 136 kB | 113 kB | 113 kB |
| 5% | 145 kB | 118 kB | 118 kB |
| 10% | 154 kB | 121 kB | 121 kB |
| 20% | 170 kB | 123 kB | 123 kB |

Parity at 0% loss, C++ netsim + fusion.h vs the Python reference on the same buildings, differing superframes: hl 0, nz 0, s2 0, s3 0, s5 0, imp5 0, imp10 0, sp5 0, sp10 0, shift5 0, shift10 0 (of 136800). The C++ fusion keeps its smoothing history from the commissioning windows, the Python recording starts fresh.

Events-only uplink per hour of healthy monitoring, by loss level:

| loss | JSONL lines/h | status | begin/change | bytes/h |
|---|---|---|---|---|
| 0% | 47.9 | 43.4 | 4.5 | 10516 |
| 5% | 145.6 | 43.4 | 102.2 | 31773 |
| 10% | 398.2 | 43.4 | 354.7 | 86847 |
| 20% | 1069.6 | 43.4 | 1026.1 | 233067 |

### False DEAD events (healthy slaves declared DEAD)

One healthy trace repeated to 20000 superframes, 10 seeds per level (1 at 0%); exact Poisson 95% interval. `STRUCTURE.md` states about 0.1 per slave-hour at 10% and about 5 at 20%.

| loss | DEAD events | slave-hours | per slave-hour | 95% interval |
|---|---|---|---|---|
| 0% | 0 | 16 | 0.000 | [0.000, 0.237] |
| 5% | 0 | 156 | 0.000 | [0.000, 0.024] |
| 10% | 19 | 156 | 0.122 | [0.074, 0.191] |
| 20% | 748 | 156 | 4.809 | [4.470, 5.166] |

### Dead slave in the network simulator (slave killed at the first event window, lossless)

| case | any alarm, LOCAL events | any alarm, BUILDING events | correct type, LOCAL | correct type, BUILDING |
|---|---|---|---|---|
| slave1 dead | 0.226 [0.213, 0.240] | 0.833 [0.812, 0.854] | 0.215 [0.203, 0.226] | 0.000 [0.000, 0.000] |
| slave2 dead | 0.250 [0.237, 0.263] | 0.822 [0.800, 0.843] | 0.244 [0.231, 0.256] | 0.000 [0.000, 0.000] |

### How a dead slave becomes visible (slave killed at superframe 40 of a healthy trace, lossless)

| killed | runs | DEAD declared, s after kill (mean / max) | first event line with `degraded_alive` = other slave, sf after kill (mean / max) | first JSONL line with `alive` false for it, sf after kill (mean / max) |
|---|---|---|---|---|
| slave1_killed | 240 | 4.2 / 4.2 (240 of 240) | 0.0 / 0 (240 of 240 runs) | 22.9 / 23 (240 of 240 runs) |
| slave2_killed | 240 | 4.2 / 4.2 (240 of 240) | 0.0 / 0 (240 of 240 runs) | 23.0 / 23 (240 of 240 runs) |

The master declares DEAD within a few seconds (latency column) and writes a dedicated `{"type":"node",...,"state":"DEAD"}` line to the JSONL log at that moment, and an `ALIVE` line on recovery. The columns above measure the older indirect signals, which remain: `degraded_alive` in a change event (which also fires for one merely lost report) and `alive: false` on the next event or periodic status line (at most 64 superframes, about 90 s). A dashboard should use the `node` line for dead-node alerts.

## Assumptions not fixed by the brief

- Primary pair: S1 and S3 on the higher floor (10), S2 on the lower (5); secondary 3/7 analogously (7 / 3). A single node runs on the recording of its floor in the same building; events on the other floor reach it through the building response.
- S1/S2 'alarm' = smoothed state non-normal (abnormal or faulty), starting each recording from normal. S3 uses the same 2-of-3 smoothing of (r >= 3.0) as H (k = 1).
- A false onset is a no-alarm to alarm transition in a healthy or noise-only recording (as in R2b). H alarms on a LOCAL or BUILDING output.
- Correct-type detection requires the right class (and slave for LOCAL) for at least one superframe of the recording; any-alarm detection requires any alarm in the recording.
- Netsim traces: 20 commissioning windows (the R2b commissioning recording) followed by one R2b recording per run, fresh fusion state per run. The per-superframe output is forward-filled from the JSONL begin/change events; COMMISSIONING and NO_DATA count as no alarm.
- Loss seeds: 1 at 0% (deterministic), 3 at 5/10/20%; per-building counts pool the seeds. The dead-slave runs kill the node at the first event window, after commissioning.

