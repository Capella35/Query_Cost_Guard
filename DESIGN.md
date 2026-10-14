# Cost Guard + Investigator — Design

What the system is meant to do, the decisions that shape it, and why. What is
built and what each eval scored lives in [README.md](README.md) (Status,
Results). The original plan is [PROJECT_BRIEF.md](PROJECT_BRIEF.md); where this
file and the brief disagree, this file is newer.

## 1. Problem and scope

Analysts write warehouse queries but cannot see what they cost; the platform
sees the cost but does not write the queries. Cost Guard sits between them
and controls query cost without taking the warehouse away from the analyst.

In scope: batch analytics queries, one mocked customer (`customer.yaml`),
local DuckDB over the TheLook data as the primary warehouse, BigQuery as an
optional second backend. Out of scope: streaming, multi-tenancy, a human
approval workflow.

## 2. Principles

These carry the design. A change that breaks one needs a decision-log entry
(section 12).

1. **The verdict is deterministic.** Read-only check, cost estimate, rules and
   caps decide allow / warn / rewrite / block. An LLM may explain a verdict or
   propose a rewrite, never change allow/block.
2. **Hard caps never adapt.** Learning can only tighten below them. A system
   that learns from history must not learn that expensive is normal.
3. **Warn by default, block rarely.** Block only what is unsafe (writes) or
   past a hard cap or a runaway join. Everything else runs with a warning. A
   tool that blocks too much gets bypassed or removed.
4. **Never offer an unverified rewrite.** Every rewrite must return the same
   results as the original on sampled inputs before it is offered, and the
   original query still runs.
5. **No silent gaps.** A rule that could not run is listed on the verdict
   (`skipped_rules`) with the reason.
6. **Measure honestly.** Labels are fixed before results are seen; post-hoc
   changes are recorded and both scores reported; a held-out set is run once;
   dollar figures are labeled MODELED.
7. **Keep customer data in the customer's environment.** Logs hold query
   shapes and numbers, never SQL text or literal values; result comparison
   returns pass/fail and a hash, never rows.

## 3. Architecture: three points in time

```
            prevent                    interrupt                  learn
  query ──► Guard (decide) ──run──► watchdog (timeout) ──► query log ──► Investigator
              │ block: never runs        │ kill: read-only only           │ proposals
              ▼                          ▼                                ▼
           verdict + trace         ExecutionResult              threshold updater
                                                                (gates → shadow →
                                                                 enforce / rollback)
                                                                          │
                                         watchdog timeouts ◄── rule store ┘
                                         report ◄── query log (behavior, workload, spend)
```

Each layer catches what the others structurally cannot: the Guard sees the
plan but not the runtime; the watchdog sees the runtime but only after
execution has started; the Investigator sees the history of a query shape.

## 4. Guard pipeline

Order matters; the first block wins.

1. **Read-only check** (sqlglot AST): exactly one SELECT/WITH, no write nodes,
   no file/network/environment functions. Fails closed on a parse error.
   Non-read-only → block, never bypassable.
2. **Cost estimate** from the warehouse: DuckDB `EXPLAIN (FORMAT JSON)`
   per-operator cardinalities including join fan-out; BigQuery dry-run bytes.
3. **Context per table:** row count, columns, partition column (section 6),
   column profiles from the registry — a lookup, never a per-query scan.
4. **Static rules** on the AST plus **plan red flags** from the EXPLAIN plan.
5. **Hard caps** for the workload class: rows (DuckDB), bytes (BigQuery).
6. **Runaway join** (cartesian finding) → block.
7. **Rewrite** for findings the catalog can fix (section 7) → rewrite.
8. Any other finding → warn. No finding → allow.
9. After the verdict: optional bypass (block → warn, only when
   `guard.allow_bypass` is true and a reason is given, traced); optional LLM
   explanation of warn/block.

Every step emits a trace span.

## 5. Cost model and policy

**Units.** DuckDB's EXPLAIN gives rows, not bytes, so on DuckDB the cost
proxy is rows processed = max(rows out of scans, largest intermediate). On
BigQuery it is dry-run bytes. Each cap applies only where its unit exists; a
DuckDB deployment never checks the bytes cap.

**Estimate corrections** (`plan_estimate.py`) use the column registry where
the planner guesses: GROUP BY output bounded by distinct counts (lowers
only); a scan whose filter the planner dropped counts every row; a remaining
range filter is RAISED, never lowered, from the column's min/max (an
underestimate could let a costly query under the cap).

**Caps** (`customer.yaml`, per workload class):

| | interactive | scheduled |
|---|---|---|
| rows processed (DuckDB) | 1M | 10M |
| bytes scanned (BigQuery) | 200 GB | 1 TB |
| runtime (watchdog) | 60 s | 600 s |

Scheduled caps stay at or above interactive ones. The evals run under the
caps their labels were written under (section 10), not these.

**Known limit:** "scanned" means rows coming out of the scans after filters.
For example, `user_id = 5` on a 2.4M-row table estimates 43 rows, even though
every row is read when nothing prunes. Changing that would shift every
estimate and possibly verdicts, so it is a separate decision.

## 6. Rules and plan checks

- **Static rules** (`rules/static_rules.py`): structure from the AST, never
  regexes. Query-level rules run once; table-level rules run per referenced
  table. "Large" = 100k+ rows. Thresholds that are judgment calls are named
  constants (near-unique GROUP BY at distinct/rows ≥ 0.5; IN lists > 100
  values).
- **Plan checks** read the EXPLAIN plan rather than the SQL text, because the
  plan knows what the text doesn't. A full in-memory sort (`ORDER_BY` with
  100k+ input rows) warns; a sort with a LIMIT is planned as `TOP_N` and a
  sort after GROUP BY sorts only the groups, so neither warns. No plan shape
  (BigQuery dry run, fallback estimate) → listed as skipped.
- **Partition column source:** the warehouse's own partitioning wins
  (BigQuery: time column, `_PARTITIONTIME` for ingestion time, or the
  integer-range column); `customer.yaml` `partitioned_tables` is the
  fallback, e.g. for DuckDB, which has no real partitions (the "partition" is
  a date column whose zone maps let DuckDB skip blocks). A disagreement is
  traced as `partition_column.mismatch`.
- **Function on the partition column is warehouse-aware.** DuckDB skips row
  groups only for comparisons on the bare column, so every wrapper warns.
  BigQuery derives partition filters through some wrappers; the parser
  records what wraps the column (innermost first), and on BigQuery the rule
  stays silent only when every wrapper is in `BIGQUERY_PRUNES_THROUGH`. Each
  entry was dry-run against its bare-column equivalent and billed the same
  bytes; measured non-pruning forms (`EXTRACT`, `UNIX_SECONDS`,
  `col + INTERVAL`, `FORMAT_TIMESTAMP('%Y-%m', …)`, `FORMAT_DATE`) and
  unmeasured ones still warn.
- **Indexes are not modeled.** Both engines are columnar and scan; their
  layout tools are partitioning and clustering. Clustering columns are read
  from BigQuery but no rule uses them yet.

## 7. Rewrites

**Catalog** (`rewrites.py`), applied in sequence: repeated subquery → CTE;
`DATE_TRUNC(unit, col) = X` / `CAST(col AS DATE) = X` → range on the bare
column; filter after a join → pushed into the derived table. No entry for
`SELECT *` or a missing date filter: any fix changes the result, so those
warn and a human chooses.

**Acceptance:** a candidate is offered only if it is read-only and differs
from the original, and `compare_results` matches on sampled inputs (fixed
seed, at most `sample_rows` per table, timeout, canonical hash). Most
candidates must also be not costlier by estimate. **Exception:** the
date-range fix only restates a filter in a form the engine can prune on, so
it returns the same rows and reads no more. DuckDB estimates the two forms
inconsistently (an equality from the distinct count, a range at a flat 20%),
so the estimate comparison would measure the estimator, not cost. On
TheLook that rewrite measured ×39–×48.

**Savings are engine-specific, and none are claimed.** The evals prove equivalence. On DuckDB
the date-range fix measured ×39–×48; the others ~1× (its optimizer already
pushes filters). On BigQuery, dry runs bill **the same bytes** for all nine
rewrites, on both the 60-day sandbox copies and the full history: BigQuery itself prunes `CAST(col AS DATE)`, `DATE(col)`,
`TIMESTAMP_TRUNC(col, …)` and `FORMAT_TIMESTAMP(…)` filters on a
`DATE(col)`-partitioned table, and already pushes filters and reuses
repeated subqueries. Only forms such as `EXTRACT(DAY FROM col)` defeat
pruning there.

## 8. The LLM's place

Optional; `NullLLM` makes zero model calls. When configured (`llm.py`):

- **Rewrite proposer:** only for flagged queries, only when the catalog has
  no fix; its SQL goes through the same acceptance as section 7.
- **Explainer:** plain-language text for warn/block, after the verdict.
- **Threshold judge:** reviews proposals that already passed the
  deterministic gates; sees aggregates only (no SQL, no fingerprint); may
  reject or hold, never enforce.

A refusal or error means no output: no rewrite, no explanation, judge holds.

## 9. Runtime and learning

- **Watchdog:** every query that runs gets a timeout — the shape's enforced
  learned threshold, else the class's hard cap. Only read-only queries reach
  execution, so killing one is safe.
- **Fingerprint:** the query with literals replaced and whitespace
  normalized; same shape, different constants → same fingerprint.
- **Investigator:** finds shapes whose recent median regressed past 1.5× their
  earlier baseline; proposes baseline p95 × 1.5 from pre-regression runs only.
- **Updater gates:** below the hard cap, tighten only, move ≤ 25% per update,
  ≥ 20 runs, not below baseline p95 → optional judge → shadow trial →
  enforce → probation. More than 5% of real runs blocked → rollback.
- **Adaptive anomaly rule** (warn when far above the shape's own history)
  exists in the replay harness only. It stays out of `decide()` until its
  false positives on drifting workloads come down (49% in the synthetic hard
  replay).

## 10. Measurement

**Evals measure verdict correctness** (`eval/run_eval.py`): exact match under
original and current labels, catch rate, and false positives split into
`false_block_rate` (costly: stops work, breeds bypasses) and
`false_warn_rate` (cheap), plus `under_block_rate`.

Integrity rules:
- Labels are fixed before a set is run. A post-hoc change is a recorded
  `label_change`, and both scores are reported.
- A held-out set is run once (the runner refuses a rerun without `--rerun`
  and marks it). Fixes made after seeing a set's results turn it into a
  tuning set, and its score has to say so.
- Evals run under frozen caps (`eval/eval_caps.yaml`, layered over
  `customer.yaml`), so a policy change does not silently re-score old labels.

**The report measures behavior and money** (`ledger.py`,
`python -m cost_guard report`), from the query log:
- **Warn follow-through:** the warned user's next run within 30 minutes —
  adopted the rewrite / changed to a clean query / re-ran unchanged / still
  flagged / nothing. The "acted" rate is an upper bound, since the next query
  may be unrelated.
- **Workload by shape:** total cost per fingerprint, with distinct users.
  Repeated waste outranks one-off mistakes.
- **Result-cache opportunity:** an identical query (keyed hash) recomputed
  within an hour, assuming the data didn't change. A cache gets built only
  if this number is large on real traffic.
- **Spend ledger:** executed / killed / blocked, bypasses, by week. Blocked
  work is an estimate of what did not run, not money saved.

Dollars are MODELED (price per TB from bytes, or the optional price per
runtime hour). A real before/after number needs billing data from a pilot.

## 11. Privacy and deployment boundary

Kept swappable behind small interfaces (`interfaces.py`): the rules, the LLM
client, the telemetry export. The query log stores:
- the fingerprint (literal-free shape) and numbers;
- `sql_hash`: an HMAC-SHA256 of the exact query, keyed per deployment
  (`COST_GUARD_LOG_KEY`, else a generated key file next to the log). It
  detects repeats without storing literals, and without the key a guessable
  literal can't be confirmed by hashing candidates;
- `user`: an opaque id exactly as the caller passes it — pseudonymize
  upstream.

Never stored: SQL text, literal values, result rows.

## 12. Decision log

| Date | Decision | Why |
|---|---|---|
| 2026-10-11 | DuckDB + TheLook primary; BigQuery optional | Zero cloud spend; the backend sits behind an interface |
| 2026-10-13 | No tool-calling Guard loop; no proposer–critic | Keep the hot path deterministic until evals show the static rules missing real cases |
| 2026-10-14 | 30-query set is no longer held out; held-out set pre-registered, then run once (29/30) | Fixes and relabels were made after seeing the 30-query results |
| 2026-10-14 | Sort check reads the EXPLAIN plan, not the SQL text | LIMIT → TOP_N and post-GROUP BY sorts are only visible in the plan |
| 2026-10-14 | Warehouse partition column first, `customer.yaml` as fallback | The YAML can drift from the real table |
| 2026-10-14 | Caps tightened (1M/10M rows, 200 GB/1 TB, 60/600 s); evals keep their own frozen caps | Production policy changes; labels encode the policy they were written under |
| 2026-10-14 | Report measures behavior, workload and modeled spend; cache deferred | Measure the opportunity before building the cache |
| 2026-10-14 | Date-range rewrite exempt from the estimate check | It restates the filter; estimates differ only through the estimator; measured ×39–×48 on TheLook |
| 2026-10-14 | BigQuery measured by dry run on sandbox copies (60-day window, dates moved) | Free; it turned "rewrite savings unproven" into a number: 0 bytes saved on BigQuery |
| 2026-10-14 | Function-on-partition rule made warehouse-aware, from a measured allowlist | Dry-run pairs showed BigQuery prunes through 9 wrapper forms; unknown forms keep warning |
| 2026-10-14 | Billing linked; full-history copies (no partition expiry) | Confirm the sandbox result on real dates: 0 of 9 again, allowlist unchanged; unprunable wrappers bill ~877× a pruned day filter on full `events` |

## 13. Open questions

- Before offering rewrites on BigQuery, the catalog must emit BigQuery SQL:
  TIMESTAMP literals for TIMESTAMP columns, no `DATE`-vs-`TIMESTAMP`
  comparisons.

- Count rows read instead of rows out of scans? It would make "scanned"
  honest, but it shifts every estimate and possibly verdicts.
- Are rewrites worth offering on DuckDB, where most measure ~1× (pushdown
  measured ×0.92 on Q08)?
- Should a NOT IN-over-NULLs rule exist? It's in the labeling guide, but no
  rule detects it.
- Clustering-aware rules, and a layout advisor that mines the query log for
  partition/cluster suggestions.
- When do the evals move to the new caps? That needs a recorded relabel of
  Q03, Q10, E06 and E10 (and the held-out guide's 5M cap).
