# Results: registry distinct-count margin (HLL_MARGIN)

Pre-registered in `eval/PREREG_hll_margin.md` (commit 67642d6), run
2026-10-17 (UTC) on data/thelook.duckdb (DuckDB 1.5.6, 7 tables, 73 columns).
The options were switched in a scratch harness; no production code was
changed for these runs.

## M1 / M2: is the GROUP BY bound a bound?

Bound = ceil(approximate distinct x margin) (+1 when the column has NULLs),
compared with the exact count (+1 likewise), over all 73 columns.

| Option | Columns where bound < exact | Worst bound / exact | Median bound / exact |
|---|---|---|---|
| A (1.05, today) | **19 of 73** | 0.872 (products.sku) | 1.054 |
| B (1.20) | **2 of 73** | 0.997 (products.sku), 0.999 (inventory_items.product_sku) | 1.200 |
| C (exact counts) | 0 (exact by construction) | 1.000 | 1.000 |

DuckDB's approximate counts against exact counts, all columns: 0.800 to 1.164.
The error is not limited to large columns: events.browser has 5 values and
is counted as 4 (0.800); products.category has 26, counted as 24. The 48 columns
with >= 1,000 values give 0.831 to 1.164, matching the earlier measurement.
No margin under 1.25 covers every column here, and a margin fitted to this
data would only cover this data.

Join side (J): the earlier distinct / 1.20 lower bound is never above the exact count
(0 of 73). Today's join term, ceil(approximate x 1.05), is above the exact count
for 51 of 73 columns, so today's join recompute runs low on those.

## M3: verdicts

| Option | TheLook tuning (30) | Regression (11) | Verdict changes vs A | Estimate changes vs A |
|---|---|---|---|---|
| A | 30/30 | 11/11 | none | none |
| B | 30/30 | 11/11 | none | none |
| C | 30/30 | 11/11 | none | none |
| C+J | 30/30 | 11/11 | none | none |
| B+J (extra, not pre-registered) | 30/30 | 11/11 | none | none |

The suites cannot tell the options apart: no registry GROUP BY bound fires on
any of the 30 tuning queries. The registry join recompute fires on two (Q08,
Q23) and moves them from ~1,532 rows (A) to ~1,590 (C), far below any cap.
The held-out suites were not run.

Test suite (346):
- B: all pass.
- C: 1 fails. `test_profiles_every_column` asserts entries are approximate,
  which pins today's setting; it is not a behaviour regression.
- B+J: 2 fail in `test_plan_correction.py`. C+J: the C failure plus 1 in
  `test_plan_correction.py`, the unit test whose hand-built registry entries
  are approximate (with an exact registry J changes nothing). Example: a 2-value column (status) gets
  the lower bound floor(2 / 1.2) = 1, so the window query's join estimate
  triples (200k to 600k rows in the unit test, ~0.5M to ~1.5M on the
  synthetic warehouse, still under the 5M cap). J is safe on approximate
  entries, but for small counts it is very loose.

## M4: cost of exact counts

Full registry build on TheLook, median of 3 (the first build of each had a
cold cache: 1.59 s and 1.96 s): approximate 0.29 s, exact 1.43 s, **4.9x**.
Not measured: tables near the 100M-row size gate, where exact
COUNT(DISTINCT) needs much more memory.

## Applying the decision rule

1. A (19 violations) and B (2) are rejected: their bound is not a bound.
   C passes.
2. C is preferred only if its build takes <= 2x the approximate build. It takes 4.9x. The
   fallback the rule names, B, was rejected in step 1. **The rule does not
   choose an option**, so per its intent the numbers go back to the owner
   unchanged.
3. J turns no labeled verdict wrong, but it has no option to attach to until
   step 2 is decided.
4. No labeled verdict changes under any option.

Nothing in production was changed. HLL_MARGIN stays 1.05.

## Out of scope, noticed

- BigQuery: `join_estimate.py` uses registry distinct counts with no margin
  in a join denominator (a count that is too high makes it run low). BigQuery's own
  approximate-count error was not measured here.
