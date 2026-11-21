# R3 fresh-block scoring (test block, scored once)

Block: seeds [6400000, 240], 240 buildings, 28.0 h of healthy + noise monitoring. Pre-registration: `r3_test_preregistration.md`. 95% intervals by bootstrapping buildings (2000 resamples, as R2b); paired differences use the same resamples. Simulation only.

Candidate (read from `fusion_eval_r3_selected.json`): **f, T=6 (b T=3), k=1**. Incumbent: **b, T=3, k=1**. For information: e: e, T=5, k=1, m: m, T=4, k=3, g: g, T=4 (b T=3), k=3 (dev-selected, not a selection).

## Decision

**Deploy rule f.**
- **Criterion 1, the paired mean-recall gain:** MET. Its 95 % interval is [+0.103, +0.122], which excludes 0 on the gain side.
- **Criterion 2, the paired false-onset increase:** MET on the point estimate, +0.25/h against a limit of 0.5. That is 46 vs 39 false onsets in 28 h, from 20 vs 16 buildings. The pre-registration judges the point estimate, but its 95 % interval [+0.04, +0.50] reaches the limit, so the alarm cost is only bounded at about that level.

Both criteria hold, so f is deployed.

**The 1/h false-onset budget is not met by either rule, on the point estimates.** f is at 1.64/h [0.75, 2.79] and b at 1.39/h [0.54, 2.54], over healthy + noise monitoring. Both intervals still include values below 1/h.

That headline figure includes 5.6 h of noise-only recordings, in which neither rule ever fires. **On healthy ambient monitoring alone, the rate that applies to a real quiet building, the rates are higher: f 2.05/h and b 1.74/h.**

As in R2b, the budget is a target that the simulated system does not reach. The comparison that the deployment rests on is the paired one: +0.113 mean recall for +0.25 false onsets per hour.

## Primary pair (floors 5/10)

| role | config | false onsets/h (healthy+noise) | vs 1/h | healthy only /h | LOCAL | BUILDING | mean |
|---|---|---|---|---|---|---|---|
| candidate | f, T=6 (b T=3), k=1 | 1.64 [0.75, 2.79] | not met | 2.05 [0.94, 3.48] | 0.666 [0.648, 0.684] | 0.803 [0.781, 0.825] | 0.734 [0.720, 0.748] |
| incumbent | b, T=3, k=1 | 1.39 [0.54, 2.54] | not met | 1.74 [0.67, 3.17] | 0.441 [0.422, 0.459] | 0.803 [0.781, 0.825] | 0.622 [0.607, 0.637] |
| info e | e, T=5, k=1 | 0.68 [0.28, 1.18] | met | 0.85 [0.36, 1.47] | 0.700 [0.683, 0.715] | 0.360 [0.335, 0.385] | 0.530 [0.514, 0.545] |
| info m | m, T=4, k=3 | 1.14 [0.54, 1.93] | not met | 1.43 [0.67, 2.41] | 0.612 [0.600, 0.624] | 0.721 [0.697, 0.744] | 0.666 [0.652, 0.680] |
| info g | g, T=4 (b T=3), k=3 | 1.32 [0.64, 2.18] | not met | 1.65 [0.80, 2.72] | 0.612 [0.600, 0.624] | 0.756 [0.732, 0.778] | 0.684 [0.670, 0.697] |

Paired difference (candidate minus b, same resamples): mean recall +0.113 [+0.103, +0.122], LOCAL +0.225 [+0.206, +0.244], BUILDING +0.000 [+0.000, +0.000], false onsets/h +0.25 [+0.04, +0.50].

Per kind: event recall [95% interval] / onset latency (superframes) / wrong-class share:

| role | config | dense | sparse | shift | x2 | x3 | x5 |
|---|---|---|---|---|---|---|---|
| candidate | f, T=6 (b T=3), k=1 | 1.000 [1.000, 1.000] / 1.304 / 0.015 | 0.202 [0.167, 0.237] / 1.010 / 0.013 | 0.796 [0.758, 0.831] / 4.120 / 0.010 | 0.433 [0.371, 0.496] / 12.077 / 0.271 | 0.975 [0.954, 0.992] / 5.056 / 0.621 | 1.000 [1.000, 1.000] / 1.462 / 0.342 |
| incumbent | b, T=3, k=1 | 0.969 [0.952, 0.983] / 2.125 / 0.010 | 0.056 [0.035, 0.079] / 1.037 / 0.010 | 0.298 [0.254, 0.342] / 3.937 / 0.008 | 0.433 [0.371, 0.496] / 12.087 / 0.246 | 0.975 [0.954, 0.992] / 5.090 / 0.600 | 1.000 [1.000, 1.000] / 1.467 / 0.308 |
| info e | e, T=5, k=1 | 1.000 [1.000, 1.000] / 1.302 / 0.013 | 0.221 [0.183, 0.258] / 1.019 / 0.006 | 0.879 [0.848, 0.906] / 2.581 / 0.004 | 0.008 [0.000, 0.021] / 6.500 / 0.117 | 0.200 [0.150, 0.250] / 13.375 / 0.583 | 0.871 [0.829, 0.912] / 5.957 / 0.963 |
| info m | m, T=4, k=3 | 1.000 [1.000, 1.000] / 3.604 / 0.017 | 0.015 [0.004, 0.025] / 7.000 / 0.000 | 0.821 [0.785, 0.854] / 3.711 / 0.004 | 0.263 [0.208, 0.321] / 13.476 / 0.221 | 0.900 [0.863, 0.938] / 8.255 / 0.421 | 1.000 [1.000, 1.000] / 3.487 / 0.079 |
| info g | g, T=4 (b T=3), k=3 | 1.000 [1.000, 1.000] / 3.604 / 0.019 | 0.015 [0.004, 0.025] / 7.000 / 0.002 | 0.821 [0.785, 0.854] / 3.711 / 0.004 | 0.312 [0.250, 0.371] / 13.133 / 0.192 | 0.954 [0.925, 0.979] / 7.402 / 0.312 | 1.000 [1.000, 1.000] / 3.358 / 0.054 |

## Secondary pair (floors 3/7)

| role | config | false onsets/h (healthy+noise) | vs 1/h | healthy only /h | LOCAL | BUILDING | mean |
|---|---|---|---|---|---|---|---|
| candidate | f, T=6 (b T=3), k=1 | 1.43 [0.61, 2.50] | not met | 1.79 [0.76, 3.12] | 0.692 [0.675, 0.708] | 0.728 [0.704, 0.753] | 0.710 [0.695, 0.725] |
| incumbent | b, T=3, k=1 | 1.18 [0.36, 2.25] | not met | 1.47 [0.45, 2.81] | 0.510 [0.491, 0.528] | 0.719 [0.694, 0.746] | 0.615 [0.599, 0.631] |
| info e | e, T=5, k=1 | 0.68 [0.32, 1.07] | met | 0.85 [0.40, 1.34] | 0.715 [0.701, 0.728] | 0.365 [0.342, 0.389] | 0.540 [0.525, 0.555] |
| info m | m, T=4, k=3 | 0.71 [0.36, 1.11] | met | 0.89 [0.45, 1.38] | 0.635 [0.625, 0.644] | 0.714 [0.688, 0.740] | 0.674 [0.660, 0.689] |
| info g | g, T=4 (b T=3), k=3 | 0.82 [0.43, 1.29] | met | 1.03 [0.54, 1.61] | 0.635 [0.625, 0.644] | 0.740 [0.717, 0.764] | 0.688 [0.674, 0.701] |

Paired difference (candidate minus b, same resamples): mean recall +0.095 [+0.085, +0.105], LOCAL +0.182 [+0.163, +0.201], BUILDING +0.008 [+0.003, +0.015], false onsets/h +0.25 [+0.11, +0.46].

Per kind: event recall [95% interval] / onset latency (superframes) / wrong-class share:

| role | config | dense | sparse | shift | x2 | x3 | x5 |
|---|---|---|---|---|---|---|---|
| candidate | f, T=6 (b T=3), k=1 | 1.000 [1.000, 1.000] / 1.365 / 0.006 | 0.212 [0.177, 0.248] / 1.297 / 0.010 | 0.863 [0.831, 0.894] / 2.754 / 0.004 | 0.275 [0.221, 0.333] / 11.712 / 0.429 | 0.908 [0.871, 0.942] / 6.321 / 0.808 | 1.000 [1.000, 1.000] / 1.808 / 0.575 |
| incumbent | b, T=3, k=1 | 0.992 [0.983, 0.998] / 1.876 / 0.006 | 0.075 [0.052, 0.098] / 1.583 / 0.010 | 0.463 [0.417, 0.508] / 2.635 / 0.004 | 0.271 [0.217, 0.329] / 11.877 / 0.408 | 0.887 [0.846, 0.925] / 6.460 / 0.800 | 1.000 [1.000, 1.000] / 1.846 / 0.537 |
| info e | e, T=5, k=1 | 1.000 [1.000, 1.000] / 1.350 / 0.002 | 0.210 [0.175, 0.246] / 1.100 / 0.002 | 0.933 [0.910, 0.956] / 2.471 / 0.004 | 0.013 [0.000, 0.029] / 13.333 / 0.125 | 0.208 [0.158, 0.258] / 12.940 / 0.621 | 0.875 [0.833, 0.912] / 5.752 / 0.971 |
| info m | m, T=4, k=3 | 0.998 [0.994, 1.000] / 3.701 / 0.010 | 0.006 [0.000, 0.015] / 2.667 / 0.008 | 0.900 [0.873, 0.927] / 3.674 / 0.010 | 0.254 [0.196, 0.312] / 15.377 / 0.267 | 0.892 [0.850, 0.929] / 8.061 / 0.454 | 0.996 [0.988, 1.000] / 3.364 / 0.113 |
| info g | g, T=4 (b T=3), k=3 | 0.998 [0.994, 1.000] / 3.678 / 0.010 | 0.006 [0.000, 0.015] / 2.667 / 0.008 | 0.900 [0.873, 0.927] / 3.674 / 0.010 | 0.287 [0.229, 0.346] / 14.725 / 0.254 | 0.933 [0.900, 0.963] / 7.844 / 0.383 | 1.000 [1.000, 1.000] / 3.288 / 0.062 |

## Limitations

- Simulation only (`building_sim`: uniform shear building, stationary band-limited excitation); nothing is measured on hardware. Real ambient drift, real excitation and the real frame will move every ratio.
- The AE part of rule f depends on the provisional node noise gate (az_std < 92.12 counts => not anomalous); without it the AE rules gave 17-18 false onsets/h on dev. The gate level must be re-measured on the real sensor.
- Commissioning assumptions: the first 20 windows per node are quiet and representative (the AE normaliser, its reference score and rule b's baseline all come from them; nothing detects a bad commissioning).
- One 28 h block (240 buildings): the absolute false-onset rate is a noisy estimate (wide bootstrap interval); the paired comparison is more precise than either absolute rate.
- Both rules are also read against the absolute 1/h budget; this is not a deployment criterion (pre-registration), the incumbent already failed it on the R2b test block.

