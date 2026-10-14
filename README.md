# Cost Guard + Investigator

Automated cost-control layer for analytics queries: a **Guard** checks queries
before they run, and an **Investigator** learns from query history to propose
tighter per-query-shape runtime thresholds.

> **Mock customer.** The pilot "customer" is fictional. Its warehouse is the public
> TheLook e-commerce dataset running **locally in DuckDB** (BigQuery is the stretch
> goal). Nothing here touches a real customer's data or spends real money.
> See `customer.yaml`.

Design principles, trade-offs and the decision log: [DESIGN.md](DESIGN.md).

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
| Cost estimate from DuckDB's plan (`EXPLAIN` cardinalities, incl. join fan-out), corrected where the plan is provably or measurably off: GROUP BY outputs bounded by registry distinct counts (only lowers), scans whose always-true filter the planner dropped count every row, remaining range filters can be RAISED (never lowered) from the registry's min/max; joins above recomputed. Limit: DuckDB's own flat 20% range guess still runs low on skewed data (TheLook 2023–2025 window: ~26k estimated vs 68k actual); labeled `explain+corrected`; labeled table-sum fallback | **built** | `backends/duckdb_backend.py`, `plan_estimate.py` |
| Hard caps per workload class, loaded from `customer.yaml` (rows on DuckDB, bytes on BigQuery) | **built** | `config.py`, `guard.py` |
| 10 static rules on the sqlglot AST: SELECT * on wide tables, missing partition filter (partition column read from the warehouse — BigQuery time, ingestion-time `_PARTITIONTIME` or integer-range partitioning — with `customer.yaml` `partitioned_tables` as the fallback, e.g. DuckDB's zone-mapped date columns; a disagreement is traced as `partition_column.mismatch`), cartesian product, function on partition column (warehouse-aware: on BigQuery only wrappers measured not to prune), filter after join/HAVING without aggregate, repeated subquery, high-cardinality GROUP BY, SUM/AVG on an identifier column (by name, or near-unique per the column registry), redundant DISTINCT (GROUP BY already unique, one-row aggregate, inside IN/EXISTS/UNION), long explicit IN lists (> 100 values; threshold is a judgment call) | **built** | `rules/static_rules.py`, `sql_ast.py` |
| Plan red flag from `EXPLAIN`: full in-memory sort of ≥ 100k rows (ORDER BY with no LIMIT — a LIMIT plans as TOP_N, a sort after GROUP BY sorts only the groups). DuckDB only; on a BigQuery dry run (no plan shape) the check is listed in `skipped_rules` | **built** | `plan_estimate.py`, `guard.py` |
| Skipped-rule reporting on every verdict and trace | **built** | `GuardVerdict.skipped_rules` |
| Column registry: a data-profiling lookup table (distinct count, NULL count, total rows, and min/max for numeric and date/time columns), one scan per table, stored in a local file next to the warehouse (`data/thelook.registry.duckdb`) — the build only reads the warehouse, and reading the registry is free; size gate, scan timeout, refresh modes (missing / stale / all), dropped-column cleanup; the Guard looks profiles up instead of scanning, and treats entries older than `max_age_days` as unverified | **built** (DuckDB; BigQuery not built) | `column_registry.py`, `scripts/build_column_registry.py` |
| Deterministic rewrite catalog: repeated subquery → CTE; `DATE_TRUNC(unit, col) = X` / `CAST(col AS DATE) = X` → range on the bare column; filter after join → pushed into the derived table. No catalog entry for SELECT * or a missing date filter: any fix changes the result. Every candidate must pass `compare_results`; all but the date-range fix must also be not costlier by estimate. The date-range fix is exempt because it only restates the filter (same rows, reads no more), and DuckDB estimates the two forms inconsistently (`CAST(col AS DATE) = X` from the distinct count, a range at a flat 20%), which used to reject it. Measured on TheLook: ×39 (`orders`), ×48 (`events`) | **built** | `rewrites.py` |
| `compare_results`: both queries on the same sampled inputs, timeout, order-insensitive / float-tolerant / NULL-consistent hash | **built** | `tools/compare_results.py`, `results.py` |
| Escape hatch: block → warn only with `guard.allow_bypass: true` + a reason; traced; never for writes | **built** | `guard._maybe_bypass` |
| JSONL tracing of every decision | **built** | `tracing.py` |
| Runtime watchdog: per-query timeout (learned threshold or hard cap), DuckDB interrupt | **built** | `runtime.py` |
| Query log (fingerprints + numbers, no raw SQL) and `get_query_history`; optional caller id, keyed hash of the exact query (repeat detection without storing literals), offered-rewrite shape, bypass flag | **built** | `runtime.py`, `tools/` |
| Workload report (`report`): warn follow-through (did the warned user adopt the rewrite, change the query, or re-run it unchanged), cost by query shape, result-cache opportunity, spend ledger (executed / killed / blocked, bypasses, by week). Dollars are MODELED and appear only when a price is set | **built** — measured on test logs only; no real users yet | `ledger.py` |
| Investigator: regression check vs a pre-regression baseline, bounded tightening, rule-store lookup, episodic memory | **built** | `investigator.py` |
| Threshold updater: deterministic gates → optional judge → shadow trial → enforce → probation, auto-rollback | **built** (offline-tested) | `thresholds.ThresholdUpdater` |
| CLI: `check`, `run` (`--user`, `--bypass-reason`), `investigate`, `report` | **built** | `python -m cost_guard` |
| LLM rewrite proposer, flag explainer, threshold judge (Claude) | **built, live eval not yet run** — tested with fakes; needs Anthropic credentials | `llm.py` |
| Adaptive anomaly rule (far above own fingerprint history) | **built in the replay harness only** — not called by `decide()` | `eval/run_replay.anomaly_flag` |
| BigQuery backend: dry-run bytes, table metadata (incl. partition column), watchdog execute | **partial** — fake-client tests only, never run on a live project; no sampled verification (rewrites fail closed) | `backends/bigquery_backend.py` |
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
   predicate links, unless provably tiny) is blocked; other findings warn.
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
| bad queries flagged (warn/rewrite/block) | 15/15 |
| good queries flagged (false positives) | 0/15 (0 blocked, 0 warned) |
| rewrites offered | 4 (Q05, Q07, Q08, Q09) — all 4 equivalent on the full data |

**Label changes, made after seeing results** (recorded per query under
`label_change` in `eval/eval_queries.yaml`; `run_eval.py` reports both
scores every run, and the original 26/30 is the honest headline):
- Q01, Q11 (`SELECT *`) and Q03 (no date filter): rewrite → warn. No
  equivalent rewrite exists — dropping columns or adding a date filter
  changes the result, so a human has to choose.
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
labeling guide in the file header. **Run once on 2026-10-14: 29/30**
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
- Hard variant (`--variant hard`, 240 simulated runs: gradual drift, noisy
  durations, a legitimately 10× month-end shape, a slow creep): adaptive
  catches 71% of bad runs with **49% false positives** — drift and creep look
  like anomalies to a per-fingerprint rule. Adaptive does not win here.

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
│   ├── investigator.py       # regression detection + bounded proposals
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
└── tests/                    # 152 tests (TheLook-dependent ones skip without data/)
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

**LLM components (optional):** set `ANTHROPIC_API_KEY` (or log in with `ant
auth login`), then `eval/run_eval.py --llm claude` and
`eval/run_judge_eval.py --llm claude`. Model: `claude-opus-5-5`. Both runs
spend real API credit.

**BigQuery stretch goal:** `cp .env.example .env`, set `GCP_PROJECT`, set a
bytes-billed cap and a timeout on the project **before running anything**, then
`python scripts/verify_schema.py --project $GCP_PROJECT` and
`scripts/make_partitioned_copies.py`.

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
- Wire the adaptive anomaly rule into `decide()` once the hard replay's false
  positives are addressed (drift-aware baselines).
- From the 2026-10-14 external review. Measurement is built (`report`),
  results are not in yet:
  - **Do warnings change behavior?** The warn follow-through rate needs real
    analysts sending `--user`.
  - **Do rewrites save money?** Needs per-byte billing; see the eval limits.
  - **Workload, not query:** shapes ranked by total cost and the result-cache
    opportunity are in the report. The cache itself waits for that
    opportunity to be measured on real traffic.
  - **Cost of being wrong:** evals now report `false_block_rate` separately
    from `false_warn_rate`, and the report counts bypasses. The 49% false
    positives are the adaptive rule's, in the synthetic hard replay; that
    rule is not in `decide()` and stays out until they come down.
  - **Spend before vs after:** needs a pilot with real billing data. The
    report's weekly ledger is the in-product proxy.
- Run and publish the live LLM evals (rewrite equivalence rate, judge
  agreement and wrongly-approved count).
- BigQuery: live dry-run validation, sampled verification, JOBS history.
- Streaming queries: out of scope for the POC.
- **Deferred explicitly:** a tool-calling (ReAct) Guard loop — only if the
  eval shows the static rules missing real cases.
