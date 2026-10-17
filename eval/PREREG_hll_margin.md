# Pre-registration: registry distinct-count margin (HLL_MARGIN)

Written 2026-10-17 (UTC), committed BEFORE any of the measurements below.

## Question

The column registry stores HyperLogLog (approximate) distinct counts. On
TheLook they were measured at -17% to +16% against exact counts (47 columns
with >= 1,000 values), but `plan_estimate.HLL_MARGIN` assumes 5%. The
registry count is used in two places in `plan_estimate`, pulling in opposite
directions:

1. GROUP BY bound: groups <= ceil(distinct x margin) (+1 for NULLs). If the
   count is too LOW, the bound is too low and the cost estimate runs low, the
   unsafe direction (a costly query can slip under the cap). A larger margin
   is safer.
2. Join recompute: rows ~ L x R / max(distinct of the keys). If the count is
   too HIGH, the estimate runs low. Today the margin is applied here as well,
   which makes that worse; a safe value here is a LOWER bound on the
   distinct count.

## Options (only DuckDB's plan correction uses the margin; BigQuery's join
estimate does not, and is not touched)

- A: today. Margin 1.05 in both places.
- B: margin 1.20 for approximate entries in the GROUP BY bound (covers the
  measured -17%: 0.83 x 1.20 = 1.0); 1.0 for exact entries.
- C: exact COUNT(DISTINCT) on DuckDB (`use_approximate: false`), so entries
  are exact and the margin is 1.0. B's margin still applies to any entry
  marked approximate.
- J (with B or C): the join uses distinct / 1.20 for approximate entries
  (a lower bound) and the exact count for exact entries; no margin upward.

## Measurements

- M1 (is the bound a bound?): every profiled TheLook column (all columns, not
  only >= 1,000 values). Exact distinct count vs the approximate one. Count
  the columns where the option's GROUP BY bound is below the exact count.
- M2 (how loose): median and max of bound / exact, per option.
- M3 (verdicts): the 30-query TheLook tuning suite (`eval/run_eval.py
  --suite thelook`), the 11 regression cases and the test suite, under A, B,
  C, and C+J. Every verdict change against A is listed with its cause, and
  checked against the suite's labels. The held-out suites are NOT run: they
  have been run once already.
- M4 (cost of C): wall time of a full registry build on data/thelook.duckdb,
  approximate vs exact, median of 3 builds each.

## Decision rule (fixed now)

1. An option with M1 > 0 is rejected: a bound that is not a bound.
2. Among the rest, C is preferred if its build takes <= 2x the approximate
   build on TheLook (M4); otherwise B.
3. J is adopted with it unless it turns a correct (labeled) verdict on the
   tuning suite into a wrong one.
4. If the chosen option turns any correct labeled verdict into a wrong one,
   nothing is changed and the numbers go back to the owner.

Results go to `eval/RESULTS_hll_margin.md`, whatever they show.
