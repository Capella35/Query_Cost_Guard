# Cost Guard + Investigator

Automated cost-control layer for analytics queries: a **Guard** checks queries
before they run, and an **Investigator** learns from query history to propose
tighter per-query-shape runtime thresholds.

> **Mock customer.** The pilot "customer" is fictional. Its warehouse is the public
> TheLook e-commerce dataset, in **BigQuery** (copies in our own project, dry
> runs only — free) and **locally in DuckDB** for offline tests. Nothing here
> touches a real customer's data; BigQuery use stays inside the free tier.
> See `customer.yaml`.

Design principles, trade-offs, the decision log and recommendations for future
improvements (section 14): [DESIGN.md](DESIGN.md).

## The problem

Analytics platforms run on shared, usage-billed warehouses. The people writing
queries can't see what those queries cost, and the people who see the cost don't
write the queries. A single query with a missing date filter, a runaway join, or a
`SELECT *` on a wide table can scan terabytes or run for an hour — and the team
only finds out when the invoice arrives.

## Status

What exists in code today. "How it works" below only describes rows marked **built**.

| Component | Status | Where |
|---|---|---|
| Read-only classifier: one SELECT/WITH statement, no write nodes, file/network/env functions denied, fails closed on parse errors | **built** | `sql_ast.read_only_violation` |
| Cost estimate from DuckDB's plan (`EXPLAIN` cardinalities, incl. join fan-out), corrected where the plan is provably or measurably off, **for basic queries only** (one table; no join, CTE, subquery or UNION; the rest keep DuckDB's own estimate): GROUP BY outputs bounded by registry distinct counts (only lowers), scans whose always-true filter the planner dropped count every row, remaining range filters can be RAISED (never lowered) from the registry's min/max; joins above recomputed. Limit: DuckDB's own flat 20% range guess still runs low on skewed data (TheLook 2023–2025 window: ~26k estimated vs 68k actual); labeled `explain+corrected`; labeled table-sum fallback | **built** | `backends/duckdb_backend.py`, `plan_estimate.py` |
| Hard caps per workload class, loaded from `customer.yaml` (rows on DuckDB, bytes on BigQuery) | **built** | `config.py`, `guard.py` |
| 9 static rules on the sqlglot AST: missing partition filter (partition column read from the warehouse — BigQuery time, ingestion-time `_PARTITIONTIME` or integer-range partitioning — with `customer.yaml` `partitioned_tables` as the fallback, e.g. DuckDB's zone-mapped date columns; a disagreement is traced as `partition_column.mismatch`), cartesian product, function on partition column (warehouse-aware: on BigQuery only wrappers measured not to prune), filter after join/HAVING without aggregate, repeated subquery, high-cardinality GROUP BY, SUM/AVG on an identifier column (by name, or near-unique per the column registry), redundant DISTINCT (GROUP BY already unique, one-row aggregate, inside IN/EXISTS/UNION), long explicit IN lists (> 100 values; threshold is a judgment call) | **built** | `rules/static_rules.py`, `sql_ast.py` |
| `SELECT *` judged on cost, not column count: flagged when the estimate is ≥ 50% of the class cap (dry-run bytes on BigQuery, rows on DuckDB), or when the shape's median runtime over ≥ 20 logged runs is ≥ 50% of the class runtime limit (nothing predicts runtime before a run, so this half needs history) | **built** | `guard.py` (`_select_star_finding`) |
| Volume rule (owner's priority 4, spend at volume): a query shape that ran ≥ 20 times in 24 h and whose modeled total (this run included) reaches $5 gets a warn, never a block, pointing at a summary table, materialized view or a scheduled run. Logged runs count their billed bytes (a cache hit bills 0); this run counts its dry-run bytes raised to BigQuery's 10 MiB-per-table minimum. Thresholds in `customer.yaml` `volume` are a judgment call; BigQuery only (skipped visibly elsewhere) | **built** — unit tests only; no real query log yet | `guard.py` (`_volume_finding`), `runtime.py` (`shape_volume`) |
| Plan red flag from `EXPLAIN`: full in-memory sort of ≥ 100k rows (ORDER BY with no LIMIT — a LIMIT plans as TOP_N, a sort after GROUP BY sorts only the groups). DuckDB only; on a BigQuery dry run (no plan shape) the check is listed in `skipped_rules` | **built** | `plan_estimate.py`, `guard.py` |
| Skipped-rule reporting on every verdict and trace | **built** | `GuardVerdict.skipped_rules` |
| Column registry: a data-profiling lookup table (distinct count, NULL count, total rows, and min/max for numeric and date/time columns — not for identifier columns (`id`, `*_id`, `*_key`, `*_code`, …), which are objects, not numbers), one scan per table, stored in a local file next to the warehouse (`data/thelook.registry.duckdb`) — the build only reads the warehouse, and reading the registry is free; size gate, scan timeout, refresh modes (missing / stale / all), dropped-column cleanup; the Guard looks profiles up instead of scanning, and treats entries older than `max_age_days` as unverified | **built** — DuckDB, and BigQuery (`--bigquery-project`: catalog from the API, each table's scan dry-run first and capped by `maximum_bytes_billed`, views profiled through the view, geography/repeated columns skipped; TheLook: 7 tables, 73 columns, 0.472 GiB, file `data/bigquery/<project>.<dataset>.registry.duckdb`) | `column_registry.py`, `scripts/build_column_registry.py` |
| Deterministic rewrite catalog: repeated subquery → CTE; `DATE_TRUNC(unit, col) = X` / `CAST(col AS DATE) = X` → range on the bare column; `EXTRACT(YEAR) = Y AND EXTRACT(MONTH) = M [AND EXTRACT(DAY) = D]` on one column → one month (day) range (T15; live dry run 0.721 → 0.058 MiB); filter after join → pushed into the derived table. No catalog entry for SELECT * or a missing date filter: any fix changes the result. Every candidate must pass `compare_results`; all but the date-range fix must also be not costlier by estimate. The date-range fix is exempt because it only restates the filter (same rows, reads no more), and DuckDB estimates the two forms inconsistently (`CAST(col AS DATE) = X` from the distinct count, a range at a flat 20%), which used to reject it. Measured on TheLook: ×39 (`orders`), ×48 (`events`) | **built** | `rewrites.py` |
| `compare_results`: both queries on the same sampled inputs, timeout, order-insensitive / float-tolerant / NULL-consistent hash | **built** | `tools/compare_results.py`, `results.py` |
| Fix hints on blocks: main cause first (a table alone over the cap, a many-to-many join key from the registry, the joins' estimated fan-out, a cartesian product), then ranked fixes (aggregate before the join, a date filter with a 30-day estimate labeled "if rows are spread evenly", column list), each marked whether it changes the result; on BigQuery, a query with a LIMIT is told that LIMIT doesn't reduce bytes billed (dry runs: `SELECT * FROM events` 367.2 MiB with or without `LIMIT 10`). Never a rewrite, never a different decision | **built** | `fix_hints.py` |
| Escape hatch: block → warn only with `guard.allow_bypass: true` + a reason; traced; never for writes | **built** | `guard._maybe_bypass` |
| Tracing of every decision: JSONL by default (spans nested, thread-safe); optional OpenTelemetry export to a local Phoenix (`PHOENIX_COLLECTOR_ENDPOINT`), OpenInference span kinds, the JSONL trace id = the Phoenix trace id. Spans carry shape ids, counts, verdicts, reasons and LLM token counts — never SQL text, prompts or rows (LLM calls are not auto-instrumented for that reason) | **built** — export tested in-memory and against a local OTLP receiver; not yet viewed in a running Phoenix | `tracing.py`, `observability.py` |
| MCP server (stdio): `check_query`, `run_query` (the only path to the warehouse; blocked queries never run), `get_table_info`, `get_query_history` (numbers only), `workload_report`. The deployment sets the workload class (`--workload`; callers may only pick a stricter one) and there is no bypass through MCP. No result rows returned; one trace per tool call, nested under the MCP SDK's own request span | **built** — tested in-process and end to end over stdio on TheLook DuckDB; BigQuery backend selectable, not run live through it yet | `mcp_server.py` |
| Runtime watchdog: per-query timeout (learned threshold or hard cap), DuckDB interrupt; on BigQuery every run also carries the class bytes cap as `maximum_bytes_billed` (a bypassed block keeps only the project ceiling) | **built** | `runtime.py` |
| Query log (fingerprints + numbers, no raw SQL) and `get_query_history`; optional caller id, keyed hash of the exact query (repeat detection without storing literals), offered-rewrite shape, bypass flag | **built** | `runtime.py`, `tools/` |
| Workload report (`report`): warn follow-through (did the warned user adopt the rewrite, change the query, or re-run it unchanged), block follow-through (fixed and ran, retried unchanged, still blocked, bypassed, nothing), cost by query shape, result-cache opportunity, spend ledger (executed / killed / blocked, bypasses, by week), Guard health (decision mix, block / flag / bypass rates, check latency p50/p95, billed ÷ estimated bytes on BigQuery), LLM usage from the trace file (calls, tokens, errors per LLM span). Dollars are MODELED and appear only when a price is set | **built** — measured on test logs only; no real users yet | `ledger.py` |
| Investigator: regression check vs a pre-regression baseline, bounded tightening, rule-store lookup, episodic memory | **built** | `investigator.py` |
| Threshold updater: deterministic gates → optional judge → shadow trial → enforce → probation, auto-rollback | **built** (offline-tested) | `thresholds.ThresholdUpdater` |
| CLI: `check`, `run` (`--user`, `--bypass-reason`), `investigate`, `investigate-anomalies`, `accept-normal`, `report` | **built** | `python -m cost_guard` |
| LLM rewrite proposer, flag explainer, threshold judge (Claude) | **built, live eval not yet run** — tested with fakes; needs Anthropic credentials | `llm.py` |
| Adaptive anomaly rule (a completed run far above its own shape's history: size clause on the two-way estimate / BigQuery bytes, drift-aware runtime clause). Runs after every completed run in `run_query`, logged on the run (`anomaly`, `anomaly_size`); never blocks or kills. The replays import the same code. Trend extrapolated at most one history span past the last run. Flagged runs stay out of the history (option B, `customer.yaml anomaly.exclude_flagged`); a person accepts a lasting change with `accept-normal <shape_id>` (CLI only, append-only audit file; later spikes still flagged) | **built** — evidence from the replays: batch, held-out hard seeds: FP 1.1%, catch 72.5%; online, held-out seeds 201–220, option B: catch 70.1%, FP 4.1% (A: 53.2%, 1.9%; [`eval/RESULTS_replay_online.md`](eval/RESULTS_replay_online.md)); live: a 5-year window over a weekly shape flagged at 262× its median size (TheLook DuckDB) | `anomaly.py`, `runtime.py` |
| Report-only investigator agent: started by an anomaly (MCP server, background) or `investigate-anomalies` (batch from the log). Fixed steps through the MCP tools (history, tables, check, neighbouring shapes, past incidents), deterministic diagnosis (size jump vs warehouse-wide slowdown vs this shape alone, recurring), report in `runs/investigations/`, incident in memory. Changes nothing. Optional Claude summary from aggregates only, off by default (`investigator.llm_max_per_day`), capped per day and cached. Cooldown per shape and a daily cap | **built** — tested with a fake warehouse and end to end over stdio on TheLook; LLM summary tested with a fake model only | `investigator_agent.py` |
| BigQuery backend: dry-run bytes, table metadata (incl. partition column), watchdog execute | **partial** — `check` run live (dry runs) on the 30-query set: 26 of 30 verdicts match DuckDB, 0 errors ([`eval/RESULTS_bq_live_check.md`](eval/RESULTS_bq_live_check.md)); `run` never executed live; rewrites verified on TEMP-table samples in a BigQuery session (live: ~30 MiB billed and ~8–10 s per verification, mostly BigQuery's 10 MiB-per-table minimum); a check takes about one dry run (~0.6 s warm: table metadata cached 10 min, independent calls overlapped); column registry built and read (the GROUP BY check now runs instead of being skipped); join fan-out estimated from the registry (`join_estimate.py`): Q13 now blocks, 26 of 30 match | `backends/bigquery_backend.py`, `scripts/bq_live_check.py` |
| BigQuery history from `INFORMATION_SCHEMA.JOBS`; telemetry exporter | **planned** (interfaces only) | `interfaces.py` |
| Human ground truth for false positives | **planned** | — |
| Result cache (serve identical repeats from a stored result) | **planned** — the report measures the opportunity first | — |
| Spend before vs after from real billing (pilot + `INFORMATION_SCHEMA.JOBS`) | **planned** | — |

## How it works

```
Guard (before execution) → watchdog execution → query log → Investigator (after execution)
        → threshold updater (gates → shadow → enforce / rollback) → rule store → watchdog
```

**Guard decisions:** allow / warn / rewrite / block, in this order:

1. A query that is not a single read-only SELECT/WITH — or that calls a
   file, network or environment function — is blocked.
2. DuckDB's own plan estimates rows processed, including join fan-out. The
   column registry corrects it where the planner guesses: GROUP BY outputs
   (bounded by distinct counts) and wide date/number range filters (raised
   from each column's min/max when the planner's flat 20% is too low; never
   lowered). Above the workload class's hard cap from `customer.yaml`, the
   query is blocked.
3. The static rules run on the parsed AST. A cartesian product (relations no
   predicate links, unless provably tiny) is blocked. So is SUM/AVG over a
   column named as an identifier (`id`, `*_id`, `*_key`, `*_code`, …): an id
   labels a row and is counted, not added; the reason carries the query with
   `COUNT(DISTINCT …)` instead (a suggestion — it answers a different
   question, so it is not an automatic rewrite). Ambiguous names (`*_no`,
   `*_num`, `*_code`), near-unique registry values and a model's unconfirmed
   label only warn: each may be a real count or amount. A column kind a person
   confirmed (`scripts/classify_columns.py --confirm`) decides: a confirmed
   identifier or category (e.g. `events.sequence_number`, a step within a
   session) blocks SUM/AVG — grouping, counting and MIN/MAX stay allowed; any
   other confirmed kind clears.
   Other findings warn.
   Column cardinality for the GROUP BY rule is a lookup in the column
   registry (profiled once, refreshed on a schedule), not a scan.
4. Findings the deterministic catalog can fix — a repeated subquery, a
   function wrapped around a date column, a filter applied after a join —
   get an equivalent rewrite, offered only if `compare_results` verifies it
   on sampled inputs. With an LLM configured, other findings may
   get an LLM-proposed rewrite under the same verification, and warn/block
   verdicts get a plain-language explanation. The LLM never changes
   allow/block.

`warn` means allow + annotate. Rules that could not run are listed on the
verdict, never silently skipped.

**Runtime:** every query that runs gets a timeout — the shape's learned
threshold if one is enforced, else the hard cap — and lands in the query log.

**Report:** `python -m cost_guard report` reads the query log and measures
what the verdicts don't: whether warned users changed their next query
(within 30 minutes), which query shapes cost the most in total, how often an
identical query is recomputed within an hour (what a result cache would
save, assuming the data did not change), and what ran, was killed or was
blocked. Blocked work is reported as an estimate of what did not run, not as
money saved.

**Investigator:** finds shapes whose recent runs regressed against their own
earlier baseline and proposes a tighter threshold: baseline p95 × 1.5,
computed only from runs before the regression, moving at most 25% per update.
Each proposal must pass deterministic gates (below the hard cap, tighten only,
bounded move, ≥ 20 runs, not below the baseline p95), then an optional LLM
judge that sees aggregates only and may reject or hold, then a shadow trial.
A candidate that would block more than 5% of real runs is rolled back, before
or after enforcement. Hard caps never adapt.

**Private-deployment seams:** static rules, the LLM client (`NullLLM` = no
model calls), and telemetry stay behind small interfaces in
`src/cost_guard/interfaces.py`. Result comparison returns pass/fail + hash,
never rows.

## Results

All numbers below regenerate from the scripts named; each has its limits next to it.

**BigQuery is the target from 2026-10-17; DuckDB stays for the unit tests
only** (free, offline, ~30 s). The eval is split in two, on BigQuery:

| Set | Queries | Use | Result |
|---|---|---|---|
| Validation, `eval/bq_validation.yaml` | 70 BigQuery-native queries: B01–B30 and T01–T40 (the old test set, merged 2026-10-17 after fixes followed its misses). The DuckDB-era Q and H queries left the BigQuery evals the same day | tune on it, rerun freely (`--suite bq-validation`) | **70/70** current labels, **62/70** original labels (8 recorded label changes), 0 false blocks, 0 false warns ([`eval/RESULTS_bq_validation.md`](eval/RESULTS_bq_validation.md)). A tuning set: every fix was made after seeing its misses |
| First test, `eval/bq_test.yaml` | 40 new BigQuery queries, labeled from the guide and data facts only, committed (`ddf0030`) before any Guard run | run ONCE 2026-10-17 | **35/40**, catch 0.917, 1 false block (T27) ([`eval/RESULTS_bq_test.md`](eval/RESULTS_bq_test.md)). Fixes for T27, T08 and T15 followed, so it is now part of validation; a fresh pool will be pre-registered and split at random |

The sections below are the earlier DuckDB-era evals, kept as history.

**Caps the evals run under:** every eval below (and the regression tests) runs
with the caps its labels were written under — 5M / 50M rows, 1 TB / 5 TB,
300 s / 3600 s — frozen in `eval/eval_caps.yaml` and overlaid on
`customer.yaml`. Production caps were tightened on 2026-10-14 to 1M / 10M rows,
200 GB / 1 TB, 60 s / 600 s. Under the new row cap, four warn-labeled full
scans of 1M–2.4M-row tables (Q03, Q10, E06, E10) would block; re-scoring the
sets against the new caps would mean relabeling them, as a recorded change.

**Guard on 30 TheLook queries** (`eval/run_eval.py`, real Kaggle TheLook data in
DuckDB; labels fixed before the Guard ran; full table in
[`eval/RESULTS_thelook.md`](eval/RESULTS_thelook.md)), no LLM:

| | |
|---|---|
| exact action match, **original labels** | 26/30 (87%) |
| exact action match, current labels (4 changed after seeing results) | 30/30 |
| bad queries flagged (warn/rewrite/block), current labels | 13/13 |
| good queries flagged (false positives), current labels | 0/17 (0 blocked, 0 warned) |
| rewrites offered | 4 (Q05, Q07, Q08, Q09) — all 4 equivalent on the full data |

**Label changes, made after seeing results** (recorded per query under
`label_change` in `eval/eval_queries.yaml`; `run_eval.py` reports both
scores every run, and the original 26/30 is the honest headline):
- Q01, Q11 (`SELECT *`) and Q03 (no date filter): rewrite → warn. No
  equivalent rewrite exists — dropping columns or adding a date filter
  changes the result, so a human has to choose.
- Q01, Q11 again (2026-10-17): warn → allow, role bad → good. Policy
  change: `SELECT *` is judged on cost, and both read far below half the
  cap.
- Q05: warn → rewrite. Same pattern as Q08 (filter after a join inside a
  subquery), which was labeled rewrite; the two labels contradicted each
  other.

**This set is no longer held out.** Every miss it showed has since been
answered — the rewrite catalog after Q07/Q08, the redundant-DISTINCT rule
after Q04, four label changes — and the queries and labels were written by
the project author. 30/30 measures fit to these queries, not how the Guard
does on queries nobody tuned against. An independent number needs a fresh,
held-out query set (ideally written by someone else) run once.

Limits: 30 queries written by the project author; rewrite speedups are
1.0–1.2× on ~3–12 ms queries — timing noise. DuckDB's optimizer already
pushes filters and shares repeated scans. **Rewrite savings are unproven:**
the evals prove rewrites are equivalent and not costlier by estimate, not
that they save money. The one large measured gain is a sargable date range
on the 2.4M-row `events` table (held-out H06, ×19).

**Rewrite savings across warehouses** (`scripts/bq_dryrun_compare.py`;
[`eval/RESULTS_bq_dryrun_full.md`](eval/RESULTS_bq_dryrun_full.md),
[`eval/RESULTS_bq_dryrun_sandbox.md`](eval/RESULTS_bq_dryrun_sandbox.md)).
BigQuery figures are dry runs (nothing executed) on TheLook copies
partitioned by `DATE(created_at)`; DuckDB figures are measured runtime.

| case | rewrite | DuckDB runtime | BigQuery bytes, sandbox (60 days, dates moved) | BigQuery bytes, full history |
|---|---|---|---|---|
| X1 `CAST(created_at AS DATE) = X` on `orders` | date range | ×38.7 | same (1.3 KB) | same (344 B) |
| X2 same on `events` | date range | ×48.4 | same (17.8 KB) | same (6.1 KB) |
| H06 `CAST(...) = X` on `orders` | date range | ×19.1 | same | same |
| Q07, H25 `DATE_TRUNC('month', …) = X` | date range | ~1× (×1.1–1.3, ms noise) | same | same |
| Q05, Q08, H16 | filter pushdown | ~1× (×0.92–1.13) | same | same |
| Q09 | CTE | ~1× (×1.08–1.12) | same | same |

**0 of 9 rewrites bill fewer bytes on BigQuery**, on either the 60-day
sandbox copies or the full history. BigQuery already prunes partitions
through `CAST(col AS DATE)`, `DATE(col)`, `TIMESTAMP_TRUNC`/`DATE_TRUNC`,
`TIMESTAMP_ADD/SUB`, `DATE_ADD/SUB` and `FORMAT_TIMESTAMP('%Y-%m-%d', …)`;
each billed exactly what its bare-column range bills. Its optimizer also
already does the pushdown and the CTE. The date-range rewrite pays off only on
DuckDB, which can't prune through those wrappers.

What the rule still protects against on BigQuery is wrappers that do defeat
pruning: `EXTRACT`, `UNIX_SECONDS`, `col + INTERVAL`,
`FORMAT_TIMESTAMP('%Y-%m', …)` and `FORMAT_DATE`. On the full `events`
history, a one-day filter written that way bills **87.7 MiB instead of
0.1 MiB** (~877×; ~68× on the 60-day sandbox copy). So the rule is now
**warehouse-aware**: on BigQuery it warns only for wrappers not measured
to prune (`static_rules.BIGQUERY_PRUNES_THROUGH`, re-checked on every run);
on DuckDB every wrapper still warns. The rewrites themselves are DuckDB SQL:
BigQuery rejects them as-is (TIMESTAMP-vs-DATE comparisons) until the
catalog emits BigQuery types.

**Held-out set** (`eval/holdout_queries.yaml`, 30 queries): queries and
labels committed 2026-10-14 **before any Guard run** on them, with the
labeling guide in the file header. **Run once on 2026-10-14: 29/30** (28/30
under the labels of 2026-10-16, 26/30 under the current ones: H11 relabeled
warn → block after a later policy change, SUM/AVG over an identifier is now
blocked; H07, H29 warn → allow, `SELECT *` judged on cost)
([`eval/RESULTS_holdout.md`](eval/RESULTS_holdout.md)) — catch rate 16/16,
1 false positive of 14 (H28, a false *warn*: a near-unique GROUP BY inside a
CTE the outer query re-aggregates; no false blocks), 3/3 rewrites equivalent
on the full data. Two correct verdicts were right for a weaker reason than
their pattern suggests: H14 (unbounded sort) and H18 (NOT IN with NULLs) were
flagged only for the missing date filter. Checks added afterwards (sort
plan flag, long IN lists, warehouse partition column) make any rerun a
tuning run; the runner refuses one without `--rerun`. Limit: written by the
same AI assistant that wrote the Guard's rules, so it is held out from the
results, not independent of the author.

**BigQuery set** (`eval/bq_queries.yaml`, 30 queries in BigQuery SQL):
queries, labeling guide and caps committed **before any Guard run** on them
(`4a2fc50`); caps scaled to TheLook's ~0.5 GB (100 MiB interactive bytes,
`eval/eval_caps_bigquery.yaml`). **Run once on 2026-10-16, dry runs only:
24/30** (22/30 under the current labels: B21 rewrite → allow, a labeling
error; B26 warn → block, the identifier policy; B24 warn → allow, `SELECT *`
judged on cost) ([`eval/RESULTS_bigquery.md`](eval/RESULTS_bigquery.md)) — 0 good
queries blocked, 1 of 14 warned (B10: the filter-after-join rule fires
although BigQuery pushes filters down itself), bad queries flagged 15/16, but
**14/16 for the right reasons**: B19 was flagged only by a bug the run
exposed (GROUP BY `u.id` checked as `events.id` — `group_by_columns` drops the
table). The other misses were listed as known gaps before the run: no
rewrite for EXTRACT / `FORMAT_TIMESTAMP('%Y-%m')` / `col + INTERVAL` filters
(B21–B23, flagged as warn instead), no sort check from a dry run (B28), join
fan-out acted on only without filters (B19). Same limit as the held-out set:
written by the assistant that wrote the rules. **After fixes made in response
(tuning, not held out): 28/30** on a marked rerun
([`eval/RESULTS_bigquery_rerun.md`](eval/RESULTS_bigquery_rerun.md)); B21's
label turned out wrong (BigQuery prunes `EXTRACT(YEAR)`, measured after the
run) and is not changed without approval; B28 (sort check) remains.

**Column kinds** (`eval/column_kinds.yaml`, pre-registered; model labels
what each column means from names, types and counts — never values;
[`eval/RESULTS_column_kinds.md`](eval/RESULTS_column_kinds.md)): name-pattern
baseline 65/73 on TheLook and 8/20 on 20 invented hard columns (2 quantities
called ids, 5 ids missed); `claude-opus-5-5` 73/73 and 20/20, two calls.
**Caveat:** the prompt named 4 of the hard columns as examples (including both
of the baseline's false ids), so it shows the model catches ids without an
id-like name (4/4 unhinted), not that it avoids calling a quantity an id.
Not wired into the Guard.

**Column kinds in use, frugally** (`scripts/classify_columns.py`): a
deterministic triage settles every column it can (non-numeric, clear id
name, clear unit word); only the rest go to a model, offline, in one call
with a column limit, cached until the column is new or its type changes. On
TheLook: 72 of 73 columns settled without a model, 1 call for
`events.sequence_number` (labeled quantity, a proposal until confirmed). A
model label never blocks on its own.

**Threshold gates + judge** (`eval/run_judge_eval.py`,
[`eval/RESULTS_judge.md`](eval/RESULTS_judge.md)): 20 labeled proposals.
Deterministic gates reject 6/6 gate-type bad proposals (loosened to p99, tiny
sample, above hard cap, 60% jump, below p95) and pass 8/8 good ones; 6 subtle
bad proposals pass the gates by design and are the judge's job. **The Claude
judge's agreement has not been measured yet** (`--llm claude` needs
credentials).

**Adaptive-threshold replay** (`eval/run_replay.py`): split the log by time
(70% history / 30% test), baselines from history, test replayed through
static-only vs static+adaptive.

- Standard variant (96 synthetic runs, executed on DuckDB): static-only 33%
  catch → static+adaptive **100%**, 0% false positives (was 67% before the
  plan estimate used the column registry's value ranges). **The anomaly is
  by construction** — labeled-bad runs are wide date windows that scan
  10–100× the data of their fingerprint's good runs — so this demonstrates
  the mechanism, not production performance. What changed: DuckDB gives
  every date-range filter the same flat 20% estimate, so wide and narrow
  windows used to look identical by rows and only runtime (often under the
  20 ms noise floor) could separate them. Now a window covering all the
  data counts every row while a 7-day window keeps the planner's 20%, so
  wide runs are ~5× their fingerprint's median and the rows clause catches
  them. Caveat: the synthetic dates are evenly spread, the best case; on
  TheLook, where orders grow over time, a partial wide window (2023–2025)
  is still estimated ~2.6× low.
- **On TheLook, DuckDB estimates vs BigQuery bytes** (`eval/run_replay_bq.py`,
  [`eval/RESULTS_replay_bq.md`](eval/RESULTS_replay_bq.md); same 96-run
  traffic shape on TheLook columns, size check only):

  | size signal for the anomaly rule | catch (static + adaptive) | false positives | 2-year vs 7-day window |
  |---|---|---|---|
  | DuckDB estimate, raise-only (what the caps use) | 33% (= static only) | 0% | ×2 |
  | DuckDB estimate, two-way (now the anomaly rule's input) | **100%** | 0% | ×104 |
  | BigQuery dry-run bytes (exact, after pruning) | **100%** | 0% | ×114–×139 |

  The raise-only estimate keeps the caps safe but hides the difference the
  anomaly rule needs: it lifts a 2-year window from the planner's flat 20% to
  ~40% and never lowers a 7-day one. The two-way estimate
  (`estimate(sql, two_way_ranges=True)`) also lowers a range to the registry's
  min/max fraction; it is used only to compare runs of one shape, never for
  a cap. The synthetic standard replay stays at 100% / 0% with it. Caveats:
  planted anomaly, small test split (6 bad / 23 good), no runtime; the
  two-way estimate assumes evenly spread values, so it can't see a window
  that is expensive only because of skew.
- Hard variant (240 simulated runs per seed: gradual drift, noisy durations,
  a legitimately 10× month-end shape, a slow creep;
  [`eval/RESULTS_replay_hard.md`](eval/RESULTS_replay_hard.md)). With a flat
  baseline (median / p95) drift and creep look like anomalies: **~50% false
  positives**. With **drift-aware baselines** (`thresholds.DriftAwareBaseline`:
  a robust trend on log runtime, flag only beyond the shape's own 0.99
  residual and 1.5× the trend-expected runtime, trend ignored when the
  history's halves disagree) on 10 held-out seeds pre-registered before the
  method existed:

  | baseline | catch | false positives |
  |---|---|---|
  | flat (median / p95) | 80% | 50.5% |
  | drift-aware | 72.5% | **1.1%** |

  Drift no longer flags (0/180 vs 176/180); creep is caught 58/60 with no
  false alarm on its still-acceptable runs (vs 118/120). The catch drop is
  the noisy shape (4/20 → 0/20), whose bad runs sit inside the noise by
  design. Limits: same generator for all seeds (guards against seed noise,
  not against the scenario design, which the author wrote); the 0.99
  quantile was chosen on the dev seed; simulated runtimes.

**Known limitation:** no human ground truth for false positives anywhere above.

## Repo map

```
├── PROJECT_BRIEF.md          # scope, architecture, eval plan
├── FIX_LIST.md               # review findings this version addresses
├── customer.yaml             # mock customer: workload-class caps, partitioned tables, bypass flag
├── eval/
│   ├── eval_queries.yaml     # 30 labeled TheLook queries (15 bad/good pairs) — no longer held out
│   ├── holdout_queries.yaml  # 30 held-out queries, pre-registered; run ONCE (--suite holdout)
│   ├── run_eval.py           # headline eval (TheLook) + --suite regression
│   ├── regression_cases.py   # 11 inline cases (regression tests, not evidence)
│   ├── run_judge_eval.py     # 20 labeled threshold proposals: gates + judge
│   ├── run_replay.py         # 70/30 adaptive replay, standard + --variant hard
│   └── RESULTS_*.md          # committed result summaries
├── src/cost_guard/
│   ├── sql_ast.py            # every structural SQL question, on the sqlglot AST
│   ├── config.py             # load_customer(), caps per workload class
│   ├── guard.py              # pre-execution decision
│   ├── runtime.py            # guard → watchdog execution → query log
│   ├── mcp_server.py         # MCP tools for AI apps; run_query = the only warehouse path
│   ├── tracing.py            # JSONL spans for every decision
│   ├── observability.py      # optional OpenTelemetry export (Phoenix)
│   ├── investigator.py       # regression detection + bounded proposals
│   ├── anomaly.py            # run vs its own shape's history (replays + production)
│   ├── investigator_agent.py # report-only agent on anomalies; changes nothing
│   ├── fix_hints.py          # ranked causes and fixes on blocks
│   ├── thresholds.py         # fingerprints, baselines, gates, rule store, updater
│   ├── llm.py                # Claude rewrite proposer / explainer / judge
│   ├── results.py            # canonical result hashing
│   ├── column_registry.py    # one-time column profiling + per-query lookup
│   ├── plan_estimate.py      # EXPLAIN cardinalities, corrected with the registry
│   ├── backends/             # duckdb (primary) + bigquery (partial)
│   ├── tools/                # compare_results, run_sample_query, metadata, history, estimate
│   └── rules/static_rules.py
├── scripts/                  # TheLook loader, column registry build, BigQuery schema check
├── demo/                     # synthetic warehouse, 4-beat CLI demo, Streamlit UI
└── tests/                    # 361 tests (TheLook-dependent ones skip without data/)
```

## Quickstart (local-first, no cloud)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# TheLook CSVs (Kaggle; public download, ~170 MB zip) into data/raw/:
#   https://www.kaggle.com/datasets/mustafakeser4/looker-ecommerce-bigquery-dataset
.venv/bin/python scripts/load_thelook_duckdb.py --data-dir data/raw --db data/thelook.duckdb  # also builds the column registry
.venv/bin/python scripts/build_column_registry.py   # refresh stale/new profiles into data/thelook.registry.duckdb (schedule it)
.venv/bin/python -m pytest -q
.venv/bin/python eval/run_eval.py
PYTHONPATH=src .venv/bin/python -m cost_guard check "SELECT * FROM users, orders"
```

`data/` is gitignored. Cost on this path is modeled from EXPLAIN row
estimates and measured runtime; every dollar figure stays labeled as modeled.

**LLM components (optional):** put `ANTHROPIC_API_KEY` in `.env` (copy
`.env.example`; `.env` is gitignored and read by `cost_guard.config.load_dotenv`,
a shell variable wins), then `eval/run_column_kinds.py`,
`eval/run_eval.py --llm claude` and `eval/run_judge_eval.py --llm claude`.
Model: `claude-opus-5-5`. These runs spend real API credit.

**BigQuery:** set a bytes-billed cap and a timeout on the project **before
running anything**, then: `scripts/bq_dryrun_compare.py --project $GCP_PROJECT
--create-copies` (partitioned TheLook copies, a few GB once),
`scripts/build_column_registry.py --bigquery-project $GCP_PROJECT` (~0.5 GB),
`scripts/bq_live_check.py --project $GCP_PROJECT` (dry runs, free).

**MCP server:** Cost Guard's tools for AI apps (Claude Code, Claude Desktop,
any MCP client), over stdio:

```bash
claude mcp add cost-guard -e PYTHONPATH=src -- .venv/bin/python -m cost_guard.mcp_server
# BigQuery instead of the DuckDB file: append  --backend bigquery  (GCP_PROJECT from .env)
```

`run_query` runs real queries (on BigQuery it bills); blocked queries never run
(no bypass through MCP; a block's `fix_hints` say what to change) and no tool
returns result rows. `--workload scheduled` sets the class for every call;
callers can only ask for a stricter one. Each call appends its trace to
`runs/traces.jsonl` and returns the `trace_id`.

**Phoenix (optional):** `pip install arize-phoenix && phoenix serve`, then
start the server with `PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006`. The
MCP request spans and the Guard's spans appear as one trace per tool call at
http://localhost:6006. Self-hosted keeps every span on the machine; a hosted
endpoint would receive span attributes (no SQL text or rows, see DESIGN.md §11).

## Demo (5 minutes, zero setup)

`demo/demo.py` builds a synthetic e-commerce warehouse in DuckDB (`/tmp`) and
walks through four beats: a `SELECT *` warning, a blocked cartesian join, a
sample-verified CTE rewrite, and the Investigator proposing a bounded
threshold from history.

```bash
PYTHONPATH=src .venv/bin/python demo/demo.py
.venv/bin/streamlit run demo/app.py     # interactive UI
```

## Roadmap

- Quantiles (an equi-depth histogram) per range column in the registry —
  only if range estimates start to matter near the cap: DuckDB's flat 20%
  guess (which the registry can only raise, from min/max) still runs ~2.6×
  low on TheLook's skewed dates.
- Wire the adaptive anomaly rule (two-way size estimate + drift-aware
  runtime baseline) into `decide()` / the runtime path. The replays now
  support it; it still needs real query history, and the query log should
  record the two-way size estimate.
- From the 2026-10-14 external review. Measurement is built (`report`),
  results are not in yet:
  - **Do warnings change behavior?** The warn follow-through rate needs real
    analysts sending `--user`.
  - **Do rewrites save money?** Needs per-byte billing; see the eval limits.
  - **Workload, not query:** shapes ranked by total cost and the result-cache
    opportunity are in the report. The cache itself waits for that
    opportunity to be measured on real traffic.
  - **Cost of being wrong:** evals now report `false_block_rate` separately
    from `false_warn_rate`, and the report counts bypasses. The adaptive
    rule's ~50% false positives in the synthetic hard replay are down to 1.1%
    on held-out seeds with drift-aware baselines; the rule is still not in
    `decide()`.
  - **Spend before vs after:** needs a pilot with real billing data. The
    report's weekly ledger is the in-product proxy.
- Run and publish the live LLM evals (rewrite equivalence rate, judge
  agreement and wrongly-approved count).
- BigQuery: live dry-run validation, sampled verification, JOBS history.
- Streaming queries: out of scope for the POC.
- **Deferred explicitly:** a tool-calling (ReAct) Guard loop — only if the
  eval shows the static rules missing real cases.
