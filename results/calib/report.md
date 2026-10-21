# CACPDF evaluation report — split `calib`

> **Preliminary, simulation-only results.** Generated automatically by `python -m cacpdf evaluate` from runs executed at generation time. 20 seeds per scenario per method (seeds 0–19). Event streams are synthetic; these numbers say nothing about real-world sensors, networks or security performance. Scenario C–G inputs were designed to exercise specific behaviours, so results reflect those designs, not a representative workload. No statistical significance is claimed.

Identical event inputs for both methods verified by hash: **yes** (140 scenario/seed pairs).

## Pooled run-level detection (all scenarios)

Positive = scenario ground truth has a staged event (B, G). Predicted positive = any INVESTIGATE/HIGH_ALERT during the run.

| method | runs | TP | FP | FN | TN | precision | recall | f1 |
|---|---|---|---|---|---|---|---|---|
| proposed | 140 | 36 | 0 | 4 | 100 | 1.0 | 0.9 | 0.947 |
| baseline | 140 | 40 | 40 | 0 | 60 | 0.5 | 1.0 | 0.667 |

## Per scenario and method

| scenario | method | runs | alert_runs | high_alert_runs | FP | FN | expected_met | goal_rate | safe_stops | steps_mean | steps_sd | distance_mean | static_exposure_mean | event_zone_steps_mean | uncertain_steps_mean | latency_mean | collisions |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | baseline | 20 | 0 | 0 | 0 | 0 | 20 | 1.0 | 0 | 19 | 0.0 | 19.0 | 0.0 | 0 | 0 |  | 0 |
| A | proposed | 20 | 0 | 0 | 0 | 0 | 20 | 1.0 | 0 | 20.3 | 2.677 | 19.0 | 0.0 | 0 | 1.3 |  | 0 |
| B | baseline | 20 | 20 | 16 | 0 | 0 | 20 | 1.0 | 0 | 34.45 | 1.572 | 19.0 | 0.0 | 0 | 0 | 0.25 | 0 |
| B | proposed | 20 | 16 | 0 | 0 | 4 | 16 | 1.0 | 0 | 38.4 | 2.326 | 23.217 | 0.0 | 0.1 | 3.65 | 1.438 | 0 |
| C | baseline | 20 | 20 | 4 | 20 | 0 | 0 | 1.0 | 0 | 33.95 | 4.548 | 19.0 | 0.0 | 0 | 0 |  | 0 |
| C | proposed | 20 | 0 | 0 | 0 | 0 | 20 | 1.0 | 0 | 35.55 | 1.276 | 19.0 | 0.0 | 0 | 16.55 |  | 0 |
| D | baseline | 20 | 20 | 0 | 20 | 0 | 0 | 1.0 | 0 | 35.2 | 1.673 | 19.0 | 0.0 | 0 | 0 |  | 0 |
| D | proposed | 20 | 0 | 0 | 0 | 0 | 20 | 1.0 | 0 | 37 | 1.654 | 19.0 | 0.0 | 0 | 18 |  | 0 |
| E | baseline | 20 | 0 | 0 | 0 | 0 | 0 | 1.0 | 0 | 19 | 0.0 | 19.0 | 6.0 | 0 | 0 |  | 0 |
| E | proposed | 20 | 0 | 0 | 0 | 0 | 20 | 1.0 | 0 | 19 | 0.0 | 23.142 | 0.0 | 0 | 0 |  | 0 |
| F | baseline | 20 | 0 | 0 | 0 | 0 | 0 | 1.0 | 0 | 19 | 0.0 | 19.0 | 18.0 | 0 | 0 |  | 0 |
| F | proposed | 20 | 0 | 0 | 0 | 0 | 20 | 0.0 | 20 | 1 | 0.0 | 0.0 | 0.0 | 0 | 0 |  | 0 |
| G | baseline | 20 | 20 | 20 | 0 | 0 | 20 | 1.0 | 0 | 36.7 | 1.261 | 19.0 | 0.0 | 0 | 0 | 0 | 0 |
| G | proposed | 20 | 20 | 20 | 0 | 0 | 20 | 1.0 | 0 | 21.1 | 1.252 | 23.728 | 0.0 | 0 | 0 | 1.05 | 0 |

Columns: `expected_met` = runs meeting every pre-written expectation check for the scenario; `static_exposure_mean` = marked-zone exposure actually incurred; `event_zone_steps_mean` = steps spent inside the event zone while the staged event was active (ground truth); `latency_mean` = steps from event onset to first alert; `uncertain_steps_mean` = steps spent in UNCERTAIN (a cost of caution).

![baseline vs proposed](comparison.png)

## ADTA trust change after injected faults (proposed runs)

Trust of the affected source immediately before/after the update triggered by the faulty record, and 10 steps later.

| fault | updates | trust_before_mean | trust_after_mean | delta_mean | trust_after_10_steps_mean |
|---|---|---|---|---|---|
| delayed | 20 | 0.808 | 0.782 | -0.026 | 0.766 |
| duplicate | 20 | 0.657 | 0.427 | -0.23 | 0.43 |
| false_detection | 224 | 0.779 | 0.772 | -0.007 | 0.732 |
| implausible_position | 20 | 0.437 | 0.237 | -0.2 | 0.239 |
| invalid_value | 20 | 0.711 | 0.461 | -0.25 | 0.464 |
| low_confidence | 20 | 0.816 | 0.82 | 0.004 | 0.83 |
| missing | 20 | 0.784 | 0.744 | -0.04 | 0.744 |
| spurious_detection | 6 | 0.814 | 0.818 | 0.004 | 0.826 |
| stale_replay | 20 | 0.807 | 0.657 | -0.15 | 0.429 |

Note: `false_detection` (D) and `spurious_detection` (A) records are well-formed; ADTA cannot see they are false from the record alone. Their trust effect comes only from disagreement handling or is positive (valid-record reward).

## Trust penalties on records without an injected-fault label

These are penalties applied to records the generator did not label as faulty (e.g. symmetric repeated-disagreement penalties on the healthy side of a conflict).

| scenario | source_id | reason | count | total_delta |
|---|---|---|---|---|
| D | range-1 | repeated_disagreement:camera-1 | 70 | -2.1 |

## Failure cases (runs not meeting the scenario's expected behaviour)

| scenario | method | failed_checks | runs | example | example_final |
|---|---|---|---|---|---|
| B | proposed | alert | 4 | B_proposed_seed0 | ARRIVED: goal reached |
| C | baseline | alert | 16 | C_baseline_seed0 | ARRIVED: goal reached |
| C | baseline | alert, no_high_alert | 4 | C_baseline_seed2 | ARRIVED: goal reached |
| D | baseline | alert | 20 | D_baseline_seed0 | ARRIVED: goal reached |
| E | baseline | static_exposure | 20 | E_baseline_seed0 | ARRIVED: goal reached |
| F | baseline | goal, safe_stop, static_exposure | 20 | F_baseline_seed0 | ARRIVED: goal reached |

## Limitations

- Software-only simulation with synthetic, scripted event streams.
- Thresholds were hand-set on the calibration split; no optimisation or sensitivity study was performed.
- Run-level detection metrics with few positive scenarios; small N.
- The baseline is deliberately simple (fixed threshold, no integrity checks).
