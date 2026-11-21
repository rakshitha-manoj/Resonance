# R3 fresh-block scoring: pre-registration

Written and committed on 2026-11-21, before the 6,400,000 block was simulated or scored by anything. That was checked by the Phase B verifier: no cache, output or code path had touched it.

## What is scored, once
- **Block:** seeds 6,400,000 to 6,400,239 (240 buildings). The recordings are the ones R2b's `simulate_chunk` defines, with the same exc ids. The primary pair is floors 5/10 and the secondary pair 3/7. Intervals are 95 %, bootstrapped by building, as in R2b.
- **Candidate:** the dev-selected configuration in `fusion_eval_r3_selected.json`: rule **f** (b OR e: separately smoothed 2-of-3 flags, OR-ed per slave before the LOCAL/BUILDING decision), T = 6 for the AE ratio, T_b = 3 for the std ratio, k = 1, with the noise gate on the AE part.
- **Incumbent:** rule **b**, T = 3, k = 1. It is scored on the same block, so the comparison is paired.
- **For information only, not a selection:** rules e, m and g at their dev-selected configurations.

## One planned change before scoring
The AE input quantiser in `ae.h` currently computes `v * (1/scale)`. TFLite's reference QUANTIZE computes `round_half_away(v / scale)`; the Phase B verifier found the two disagree at near-half points.

The plan:
1. Change it to `v / scale` before scoring.
2. Recompute the dev table from the cached per-window inputs.
3. Re-run the same selection rule.

If that re-run selects a different configuration, the re-run's selection is the one scored, and both dev tables are reported.

## Deployment decision (decides `CHOSEN_RULE` in the firmware)
Deploy **f** if and only if both of these hold on the fresh block (primary pair):
1. **Recall gain is real:** the paired bootstrap 95 % interval of mean(LOCAL recall, BUILDING recall) for f minus b excludes 0.
2. **Bounded alarm cost:** the paired increase in false onsets per hour (f − b), over healthy + noise monitoring, has a point estimate ≤ 0.5 /h.

Otherwise deploy **b**. The AE score is then still published as an anomaly indicator on the dashboard and stays out of the alarm decision.

## How the absolute 1 /h budget is read
Both rules are reported against the 1 /h false-onset budget as **met** or **not met**. This is not a deployment criterion, because the incumbent b already failed it on the R2b test block (2.04 /h, interval [1.04, 3.25]).

f's flags are a superset of b's, so f's rate will be at least b's on the same block. **We expect both to exceed 1 /h,** and that will be stated plainly. The headline comparison is the paired one: the recall gain against the extra false onsets per hour.
