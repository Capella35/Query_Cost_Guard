# Pre-registration: online anomaly replay, option A vs option B

Written 2026-10-17, committed BEFORE any run on the held-out seeds below.

## Question

When a run is flagged by the anomaly rule, should it stay in its query
shape's history? A keeps it (what the batch replays evaluated), B leaves it
out. The owner chose B and asked for this check.

## Method (code as committed with this file)

- `eval/run_replay.py --variant online-heldout`, using the production
  `cost_guard.anomaly.check_run` (drift-aware rule, cold start 20 runs,
  trend extrapolated at most one history span), `exclude_flagged` False (A)
  or True (B). Nothing else differs between A and B.
- Traffic: `gen_online_traffic(seed)` = the hard replay's four shapes
  (drift, noisy, wide_spread, creep; generator unchanged) plus `new_peak`,
  added for B's known risk (a legitimate pattern that starts after the
  cold start). new_peak has its own RNG; the four shapes are identical to
  the hard replay's for the same seed.
- Online: every run is checked against all earlier runs of its shape, then
  joins the log. Scored on the last 30% of runs by time.

## Seeds

- Held-out: 201-220 (20 seeds). Never run before this file was committed.
- Development: seed 7 only. Seen before registration:
  A catch 0.429 / false positives 0.012; B catch 0.571 / false positives 0.048
  (B: +1 creep run caught; +2 wide_spread and +1 new_peak false positives).

## Metrics

Primary: catch rate and false-positive rate, A and B, pooled over the
held-out seeds. Also per shape. No pass/fail threshold is set: the result
is reported as it comes out, and the owner decides with the numbers.

## Run once

The held-out run writes `eval/RESULTS_replay_online.md`; the script refuses
a second run unless `--rerun`, and a rerun is labeled a tuning run.

## Known limits

Simulated runtimes and synthetic labels, written by the same author as both
options; the seeds guard against fitting one seed's noise, not against
fitting the scenario design. new_peak was designed to show B's weakness and
`noisy`/`creep` to show A's, so the shape mix decides the pooled numbers —
read the per-shape table.
