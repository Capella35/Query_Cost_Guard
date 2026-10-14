# Cost Guard + Investigator

Automated cost-control layer for analytics queries: a **Guard** checks queries
before they run, and an **Investigator** learns from query history to propose
tighter per-query-shape runtime thresholds.

> **Mock customer.** The pilot "customer" is fictional. Its warehouse is the public
> TheLook e-commerce dataset running **locally in DuckDB** (BigQuery is the stretch
> goal). Nothing here touches a real customer's data or spends real money.
> See `customer.yaml`.

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
| 8 static rules on the sqlglot AST: SELECT * on wide tables, missing partition filter, cartesian product, function on partition column, filter after join/HAVING without aggregate, repeated subquery, high-cardinality GROUP BY, SUM/AVG on an identifier column (by name, or near-unique per the column registry) | **built** | `rules/static_rules.py`, `sql_ast.py` |
| Skipped-rule reporting on every verdict and trace | **built** | `GuardVerdict.skipped_rules` |
| Column registry: a data-profiling lookup table (distinct count, NULL count, total rows, and min/max for numeric and date/time columns), one scan per table, stored in a local file next to the warehouse (`data/thelook.registry.duckdb`) — the build only reads the warehouse, and reading the registry is free; size gate, scan timeout, refresh modes (missing / stale / all), dropped-column cleanup; the Guard looks profiles up instead of scanning, and treats entries older than `max_age_days` as unverified | **built** (DuckDB; BigQuery not built) | `column_registry.py`, `scripts/build_column_registry.py` |
| Deterministic rewrite catalog: repeated subquery → CTE; `DATE_TRUNC(unit, col) = X` / `CAST(col AS DATE) = X` → range on the bare column; filter after join → pushed into the derived table. No catalog entry for SELECT * or a missing date filter: any fix changes the result | **built** | `rewrites.py` |
| `compare_results`: both queries on the same sampled inputs, timeout, order-insensitive / float-tolerant / NULL-consistent hash | **built** | `tools/compare_results.py`, `results.py` |
| Escape hatch: block → warn only with `guard.allow_bypass: true` + a reason; traced; never for writes | **built** | `guard._maybe_bypass` |
| JSONL tracing of every decision | **built** | `tracing.py` |
| Runtime watchdog: per-query timeout (learned threshold or hard cap), DuckDB interrupt | **built** | `runtime.py` |
| Query log (fingerprints + numbers, no raw SQL) and `get_query_history` | **built** | `runtime.py`, `tools/` |
| Investigator: regression check vs a pre-regression baseline, bounded tightening, rule-store lookup, episodic memory | **built** | `investigator.py` |
| Threshold updater: deterministic gates → optional judge → shadow trial → enforce → probation, auto-rollback | **built** (offline-tested) | `thresholds.ThresholdUpdater` |
| CLI: `check`, `run`, `investigate` | **built** | `python -m cost_guard` |
| LLM rewrite proposer, flag explainer, threshold judge (Claude) | **built, live eval not yet run** — tested with fakes; needs Anthropic credentials | `llm.py` |
| Adaptive anomaly rule (far above own fingerprint history) | **built in the replay harness only** — not called by `decide()` | `eval/run_replay.anomaly_flag` |
| BigQuery backend: dry-run bytes, table metadata, watchdog execute | **partial** — fake-client tests only, never run on a live project; no sampled verification (rewrites fail closed) | `backends/bigquery_backend.py` |
| Deterministic fixers for SELECT *, missing/unsargable partition filters, filter pushdown | **planned** (today only the LLM proposer can offer these) | — |
| BigQuery history from `INFORMATION_SCHEMA.JOBS`; telemetry exporter | **planned** (interfaces only) | `interfaces.py` |
| Human ground truth for false positives | **planned** | — |

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

**Guard on 30 TheLook queries** (`eval/run_eval.py`, real Kaggle TheLook data in
DuckDB; labels fixed before the Guard ran; full table in
[`eval/RESULTS_thelook.md`](eval/RESULTS_thelook.md)), no LLM:

| | |
|---|---|
| exact action match, **original labels** | 25/30 (83%) |
| exact action match, current labels (4 changed after seeing results) | 29/30 (97%) |
| bad queries flagged (warn/rewrite/block) | 14/15 (93%) |
| good queries flagged (false positives) | 0/15 |
| rewrites offered | 4 (Q05, Q07, Q08, Q09) — all 4 equivalent on the full data |

**Label changes, made after seeing results** (recorded per query under
`label_change` in `eval/eval_queries.yaml`; `run_eval.py` reports both
scores every run, and the original 25/30 is the honest headline):
- Q01, Q11 (`SELECT *`) and Q03 (no date filter): rewrite → warn. No
  equivalent rewrite exists — dropping columns or adding a date filter
  changes the result, so a human has to choose.
- Q05: warn → rewrite. Same pattern as Q08 (filter after a join inside a
  subquery), which was labeled rewrite; the two labels contradicted each
  other.

Remaining miss: Q04 (redundant DISTINCT) is not detected at all.

Limits: 30 queries written by the project author; rewrite speedups are
1.0–1.2× on ~3–12 ms queries — timing noise. DuckDB's optimizer already
pushes filters and shares repeated scans; the catalog's benefit is clearer
on engines billed per byte scanned or without those optimizations.

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
│   ├── eval_queries.yaml     # 30 labeled TheLook queries (15 bad/good pairs)
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
└── tests/                    # 135 tests (TheLook-dependent ones skip without data/)
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
.venv/bin/python -m cost_guard check "SELECT * FROM users, orders"
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
- Run and publish the live LLM evals (rewrite equivalence rate, judge
  agreement and wrongly-approved count).
- BigQuery: live dry-run validation, sampled verification, JOBS history.
- Streaming queries: out of scope for the POC.
- **Deferred explicitly:** a tool-calling (ReAct) Guard loop — only if the
  eval shows the static rules missing real cases.
