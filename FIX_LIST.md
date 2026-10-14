# Cost Guard: prioritized fix list

Source: review of `cost-guard-demo_2.zip` (read all files, ran tests, eval, replay, and edge-case probes).
Baseline that reproduces: 19 unit tests pass; `run_eval.py` 11/11; `run_replay.py` static-only 33% catch vs static+adaptive 100%, 0% false positives (synthetic labels).

Effort: S = under an hour, M = half a day, L = a day or more. Each item has an acceptance test; write the test first.

## Phase 0: make the repo tell the truth (S, do first)

**F0.1 README claims vs code.** README says each threshold proposal "passes safety gates, an LLM judge, and a shadow-mode trial" with auto-rollback. In code, `ThresholdUpdater.apply()` raises `NotImplementedError`, no LLM is ever called, and `__main__.py` (the CLI) raises `NotImplementedError`.
Fix: add a "Status" table to the README (built / stubbed / planned) and reword the claims. Move rollback, judge, and the BigQuery backend to "planned".
Accept: every sentence in "How it works" maps to a "built" row.

**F0.2 Config is never loaded.** `customer.yaml` (hard caps per workload class, price per TB) is read by nothing. Caps are hard-coded as `{"max_rows_scanned": 5_000_000}` in four places, and `workload_class` is only a trace attribute.
Fix: one `load_customer()` function; `decide()` takes caps from the chosen workload class; remove the hard-coded dicts.
Accept: changing a cap in `customer.yaml` changes a verdict in a test; interactive vs scheduled produce different caps.

**F0.3 Silent rule skipping.** `no_function_on_partition_column` and `filter_before_join` raise `NotImplementedError`, and `decide()` swallows it, so coverage looks complete when it is not.
Fix: record `skipped_rules` on `GuardVerdict` and in the trace; list them in the README Status table until implemented.
Accept: a verdict for any query shows which rules did not run.

## Phase 1: Guard correctness (found by probing; M to L)

**F1.1 Replace regex SQL parsing with a real parser (L, unlocks F1.2 to F1.5).** `sqlglot` (deterministic AST, DuckDB and BigQuery dialects) for: join detection, group-by columns, subquery extraction and the CTE rewrite, partition-filter detection, function-on-column detection. Not an LLM; this is a parsing problem. Verify the current `sqlglot` API against its docs before coding.
Accept: all existing 19 tests and 11 eval cases still pass on the AST implementation.

**F1.2 Cross join logic is both too strict and too loose.**
- `CROSS JOIN ... WHERE a.id = b.id` is blocked (benign; it is a join with a condition).
- `CROSS JOIN` with a one-row aggregate CTE (share-of-total pattern) is blocked (common and safe).
- `FROM users u, orders o WHERE u.age_band = 3` is NOT flagged as a cartesian product; the "has any WHERE" check treats an unrelated filter as a join condition.
Fix: after F1.1, flag a cartesian product only if no predicate links the two relations, and exempt relations that are provably tiny (one-row aggregate, or small by table metadata).
Accept: three probe queries above give allow, allow, block (or warn with a cartesian finding).

**F1.3 The cost estimate cannot see join fan-out (M).** `estimate()` sums the row counts of referenced tables, so a cartesian product or self-join explosion never trips the 5M-row cap (users x orders = 1.05M by this measure). The events self-join on `user_id` only got a warning from the date-filter rule.
Fix: use the engine's cardinality estimates (DuckDB `EXPLAIN` JSON; BigQuery dry run gives bytes) and keep the table-sum only as a fallback, labeled as such.
Accept: unconstrained self-join and cartesian queries exceed the cap in a test.

**F1.4 `compare_results` can cost as much as the query it checks (M).** It runs the full original query: `LIMIT` on an outer wrapper does not bound an aggregate. Measured: a heavy aggregate took 0.6 s for two sample runs on 1M rows, and on BigQuery this would bill for the full scan.
Fixes: (a) run both versions against a sampled copy of the inputs with a row/bytes cap and a timeout; (b) hash order-insensitively unless the query has `ORDER BY`; (c) round floats to a tolerance (docstring says TODO); (d) treat NULLs consistently.
Accept: unordered-but-equal results pass; a heavy query's verification stays under a stated fraction of its full cost.

**F1.5 `is_read_only` has false positives and a gap (S after F1.1).** It rejects `WHERE status = 'create'` (keywords inside string literals), and it allows file-reading functions (`SELECT * FROM read_text('/etc/hostname')` returned allow).
Fix: AST check: exactly one statement, root is SELECT or WITH, no write nodes, and a deny list for file/network functions (`read_csv`, `read_text`, `read_parquet`, `glob`, and similar). Fail closed on parse errors.
Accept: literal case passes; `read_text` and multi-statement inputs block.

## Phase 2: evidence you can defend (M)

**F2.1 Wire the realistic eval set.** `eval/eval_queries.yaml` (30 TheLook queries with labels) is consumed by nothing; `run_eval.py` uses 11 inline cases written alongside the rules, so 100% is expected, not evidence.
Fix: load the YAML, run all 30 through `decide()` against DuckDB-loaded TheLook, and report accuracy per pattern (`guard_action_expected` vs actual). Keep the 11 inline cases as regression tests, not as the headline.
Accept: a results table by pattern, including honest misses.

**F2.2 Report limits next to the replay result.** The 33% to 100% result comes from 96 generated runs where "bad" means 10 to 100x the data by construction. Keep it as a mechanism demo and say so beside the number (the README already says labels are synthetic; add that the anomaly is by construction).
Add a harder replay: gradual drift, noisy durations, and a fingerprint with a legitimately wide spread, and report false positives there.
Accept: a replay variant where adaptive does NOT hit 100%, reported as is.

**F2.3 Investigator ratchet bug.** `investigate()` hard-codes `old_threshold=30.0` and sets `new_threshold = p95 * 1.5` from stats that include the regressed runs, so a slowdown inflates the threshold it should be catching.
Fix: compute the baseline from the pre-regression window only; read the old threshold from the current rule store; enforce the per-update movement limit and the hard caps in code.
Accept: a test where recent slow runs raise the proposed threshold fails; a bounded tightening passes.

## Phase 3: add the LLM where it earns its place (after Phases 0 to 2)

**F3.1 Rewrite proposer behind the verifier (M).** Implement `LLMClient.propose_rewrite`; the Guard offers a rewrite only if the fixed `compare_results` (F1.4) passes. Keep `NullLLM` as the no-LLM mode.
Accept: eval reports rewrite equivalence rate (target 100% of offered rewrites) and savings; a wrong rewrite is rejected in a test.

**F3.2 Threshold-proposal judge with its own eval (M).** Judge sees aggregates only (counts, percentiles, rule names), never SQL text. Build ~20 labeled proposals (good, plus bad: loosened to p99, tiny sample, above hard cap). Report agreement and, above all, wrongly approved bad proposals. Deterministic gates run before the judge; the judge may reject or hold, never enforce.
Accept: a table of judge decisions vs labels; zero bad proposals pass the deterministic gates.

**F3.3 Plain-language explanation of flagged queries (S).** Call the LLM only on warn/block, after the deterministic verdict. Evaluate on a handful of cases by reading them; no metric needed.

## Phase 4: finish the loop (L)

**F4.1 Implement `ThresholdUpdater.apply()`** with shadow mode ("would have blocked" logging) and auto-rollback, plus an offline test that replays a bad threshold and triggers rollback.
**F4.2 Runtime enforcement.** The brief promised a watchdog or statement timeout; none exists (the replay's timeout is the harness, not the Guard). Add a per-query timeout for read-only queries on DuckDB (interrupt) and note the BigQuery equivalent.
**F4.3 Implement the stubbed tools and CLI** (`run_sample_query`, `get_query_history`, `get_table_metadata`, `__main__`), or delete them and the references.
**F4.4 BigQuery backend** (dry-run bytes) as the stretch goal, behind the same backend interface.

## Suggested order
F0.1 to F0.3 -> F1.1 -> F1.2, F1.5 -> F1.3, F1.4 -> F2.1 -> F2.3 -> F3.1, F3.2 -> F4.x.

## Definition of done for the portfolio version
- README status table matches the code.
- 30-query eval reported by pattern, with misses shown.
- At least one LLM component with its own small, honest eval.
- Replay includes a case where adaptive does not win.
