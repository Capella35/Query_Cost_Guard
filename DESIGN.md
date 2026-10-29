# Query Preflight + Investigator — Design

What the system is meant to do, the decisions that shape it, and why. What is
built and what each eval scored lives in [README.md](README.md) (Status,
Results). The original plan is [PROJECT_BRIEF.md](PROJECT_BRIEF.md); where this
file and the brief disagree, this file is newer.

## 1. Problem and scope

Analysts write warehouse queries but cannot see what they cost; the platform
sees the cost but does not write the queries. Query Preflight sits between them
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
8. **Decide before the query runs.** Verdicts come from pre-run evidence:
   dry-run bytes, registry estimates and the query log. The watchdog
   timeout and `maximum_bytes_billed` are backstops, not the policy. "The
   warehouse handles it" or "it bills nothing" is not a reason to allow:
   the T08 live test (a filtered fan-out join) produced 122.5M rows in
   14.3 s and billed 10 MiB, so neither a kill nor the bill would have
   stopped it.

### Policy priorities

Owner's order (2026-10-17). When two goals conflict, the higher one wins:

1. **Never a wrong answer.** A rewrite must return the same rows.
2. **No false blocks on good queries.** Analysts stop trusting a guard that
   blocks good work.
3. **Catch runaways before they run:** bytes over the cap, cartesian
   products, join fan-out.
4. **Total spend at volume.** Cost is per query × how often it runs. Billed
   bytes on BigQuery are at least 10 MiB per table read, so a "cheap" query
   run 10,000 times is a real line on the bill.
5. **The Guard's own cost and speed.** Dry runs are free; rewrite
   verification bills (~30 MiB each, remembered per query shape); a check
   takes about one dry run.

### When the Guard and the policy disagree

The policy is the labeling guide (the header of `eval/bq_validation.yaml`,
written by `scripts/build_bq_validation.py`; the copy in `eval/bq_test.yaml`
stays as registered) plus the decision log (section 12). A code
choice that departs from the guide is written into the guide as an
exception, never only in a docstring. For each disagreement:

1. **Find the cause first**, from the trace: the findings, the estimate and
   the metadata the Guard used. Classify it only after that.
2. **Classify it:**
   - **Bug.** The code doesn't do what the policy says. Fix the code and add
     a test; the policy is unchanged.
   - **Guide wrong.** The policy text is wrong or stale. Change the guide
     with the owner's approval, record the label change (`label_from` /
     `label_change`) and report both scores.
   - **Deliberate difference.** The code departs from the guide on purpose.
     The owner decides which wins; the result goes into the decision log
     and the guide the same day.
   - **Missing feature.** The policy is right and not built yet. Backlog it
     and report the miss.
3. **Who decides.** Bugs may be fixed directly, and said so. Anything that
   changes what blocks or warns is the owner's call.
4. **Scores.** A run-once result is never changed. A fix after it makes
   that set a tuning set; only a fresh set measures the fix.

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

**Join fan-out on BigQuery** (`join_estimate.py`). A dry run counts bytes
read, so a join that multiplies rows looks cheap (Q13: ~10 MiB read, ~100M
rows produced). The Guard estimates join output from the registry:
rows(L) × rows(R) / max(distinct keys), per SELECT scope of tables joined on
column equalities. It blocks over the rows cap only when the join fans out
(≥2× its largest input, so lookup joins never count) and nothing else
filters the inputs. Filters on the partition column are applied as the
share of partitions a dry run reads (it follows the real, growing
distribution of the data; evenly spread dates would overestimate old periods
and could block wrongly). Filters `col = value` and `col IN (values)` on a
profiled column keep 1/distinct (n/distinct) of the rows, a uniform
assumption (T08: `country = 'Japan'` estimated 1/16, ~2.5% real; ~333M rows
estimated, 122.5M real, measured live). Other filters (ranges, LIKE, a join
on an expression) leave the estimate far high (Q28: 8.6M estimated, 85k
real), so those estimates are only traced; the labeling guide lists this as
an exception. On skewed
join keys the formula runs low (57× on a `city` join), so it misses blocks
rather than making wrong ones. Unknown
inputs (subqueries, CTEs, missing registry entries) give no estimate.

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
  constants (near-unique GROUP BY at distinct/rows ≥ 0.8, the column judged
  with its own table; IN lists > 100 values).
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

**One round per query.** A catalog fix removes the pattern it fixed, so its
output passes the Guard as allow or warn (tested for all three fixers). The
model has no such stop: given its own output, it can offer another
"improvement" every time. So `run_query` logs the shape of every rewrite
offered (the verdict's own, or the model's verified offer from `assist()`)
as `rewrite_fingerprint` and passes those shapes to `decide()`; a query with
one of those shapes gets a warn and no second rewrite from either. `check`
keeps no log, so it stays stateless.

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

**Verdict first (owner's principle, 2026-10-23).** `guard.decide()` takes no
model: the verdict is deterministic, and the model can neither change nor
delay it. The model's help comes after, from `guard.assist()`, and only
where the verdict failed (warn or block); it fills `suggestion`,
`explanation` and `assist_notes`, never the decision or the reason
(asserted in `assist()`, tested in `tests/test_verdict_first.py`). The CLI
prints the verdict line first and the help after; the MCP server returns the
verdict at once and the help through `get_assistance(assist_id)`;
`run_query` decides, runs, then asks for help.

- **Improved query / rewrite proposer:** warn and block. One the model says
  keeps the result goes through the same acceptance as section 7 and, on a
  warn, is OFFERED (`suggestion.verified_rewrite`): the decision stays warn.
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
  runs in the package since 2026-10-17 (`anomaly.py`, called by
  `runtime.run_query` after each completed run; c49f968): it flags and logs,
  kills nothing, and is not part of `decide()`. Its runtime check uses a **drift-aware
  baseline** (`thresholds.DriftAwareBaseline`): a robust Theil–Sen trend on
  log runtime, so steady growth is expected; a run is flagged only when it
  beats the trend-expected runtime by more than the shape's own 0.99
  residual (recurring peaks such as month-end runs are normal) and by
  `REGRESSION_RATIO` (1.5). The trend is ignored when the history's halves
  disagree on it, so a regression that began inside the history isn't
  learned as normal. In the synthetic hard replay, on 10 held-out seeds:
  false positives fall from 50.5% (flat median / p95) to 1.1%, catch from 80%
  to 72.5% (the noisy shape's bad runs sit inside the noise by design). It
  stays out of `decide()` until it has real history. Its size check is only as good as its input, so it gets a
  **two-way** estimate (`estimate(sql, two_way_ranges=True)`) while caps keep
  the raise-only one. Caps must never see an underestimate; the anomaly rule
  compares runs of one shape, so it needs relative accuracy. On TheLook a
  2-year window reads ~2× a 7-day one raise-only (rule catches none), ×104
  two-way and ×114–×139 in BigQuery dry-run bytes (rule catches all, no false
  positives, with either).

## 10. Measurement

### What the tests are for

One goal: keep analytics query costs under control without getting in
analysts' way. Each group of tests answers one question about that.

1. **Does the Guard make the right call before a query runs?** The 30-query
   eval, the held-out set and the regression cases check allow / warn /
   rewrite / block, and count the kinds of mistake separately: a wrongly
   blocked query stops someone's work; a wrong warning costs a glance.
   *Status:* good on the project's own queries (held-out 29/30 on DuckDB;
   pre-registered BigQuery set 24/30, no good query blocked); unknown on
   queries written by someone else.
2. **When it offers a cheaper version, is it the same answer, and really
   cheaper?** Rewrite verification and the BigQuery dry runs. Same answer:
   yes, every time. Cheaper: depends on the warehouse — up to ×48 on DuckDB,
   0 bytes saved on BigQuery, which already does those optimizations. Hence
   the warehouse-aware rules.
3. **Can it learn what's normal for each recurring query, and notice when
   something goes wrong, without crying wolf?** The replays. With DuckDB's
   raw estimates the anomaly rule missed oversized queries; with a flat
   baseline it flagged normal growth about half the time. With two-way
   estimates and drift-aware baselines it catches most bad runs at ~1% false
   alarms. *Status:* works on simulated data; not tried on real history or
   wired into the live path.
4. **Does it change behavior and save money?** The workload report: do
   warned analysts fix their queries, which repeated queries cost most, what
   a result cache would save, spend over time. *Status:* measurement built,
   no answers yet; needs real users and real billing data.

Underneath all four: **can the numbers be trusted?** Labels are fixed
before results, label changes are recorded with both scores reported,
held-out sets run once, eval caps are frozen, and tuned choices are named.
A number quietly fitted to the test is useless for deciding anything.

In short: 1–3 show the machinery works as designed, mostly on the project's
own or synthetic data. 4 — whether it saves real money in real use — only a
pilot can answer.

### Evals and the report

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
`python -m query_preflight report`), from the query log:
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
  (`QUERY_PREFLIGHT_LOG_KEY`, else a generated key file next to the log). It
  detects repeats without storing literals, and without the key a guessable
  literal can't be confirmed by hashing candidates;
- `user`: an opaque id exactly as the caller passes it — pseudonymize
  upstream.

Never stored: SQL text, literal values, result rows.

Traces (`runs/traces.jsonl`, and Phoenix when export is on) hold span names,
timings and attributes: shape ids (a hash of the fingerprint), counts,
verdicts, reasons, LLM token counts. Not the fingerprint text, SQL, prompts or
rows; LLM calls are not auto-instrumented because an instrumentor would export
the prompt, which holds the query. A self-hosted Phoenix keeps everything on
the machine; a hosted endpoint is a deployment decision.

The MCP server returns the caller's own information (verdicts, its query's
fingerprint and rewrite, counts, bytes, durations) and never result rows;
query history drops the caller id and the keyed query hash.

### Requirement: scheduled column-registry refresh

A deployment MUST run `python -m query_preflight registry refresh` on a schedule
(`--mode stale`). The Guard's join, GROUP BY and value-filter estimates read
the registry; its entries go unverified after `max_age_days` (35) and the
rules that need them are then skipped, so without a schedule the Guard
slowly loses those checks. Views on outside data drift faster: TheLook's
`users`, `products`, `distribution_centers` are views on Google's public
dataset, regenerated daily (Colombia 21 → 10 users in two days).

Scheduled here monthly (owner's call, 2026-10-29), as a DRY RUN by default:
launchd runs `scripts/schedule/refresh_registry.py` on the 1st at 09:00
(`scripts/schedule/install_launchd.sh`; `uninstall` removes it). With
`DRY_RUN = True` it only logs which tables a refresh would profile (BigQuery
metadata, not billed); set `DRY_RUN = False` in that file for real refreshes
(`--mode all`: ~610 MiB a run). Until then the registry ages and its rules go
unverified after 35 days; refresh on demand. A deployment would use Cloud
Scheduler → a Cloud Run job with the registry file in a bucket where the
Guard runs. A plain scheduler, not an agent: the refresh is
deterministic. Cost, dry-run measured: a full re-profile bills ~668 MiB
($0.004 MODELED); a stale-mode run on a fresh registry processes 0 bytes.
`query_preflight registry status` (or the MCP tool `registry_status`) shows each
table's age and the last run; a running Guard reloads the file when it
changes.

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
| 2026-10-14 | Adaptive replay re-run on TheLook with DuckDB estimates vs BigQuery dry-run bytes | The synthetic replay's 100% leaned on evenly spread dates; on real dates DuckDB's estimate can't separate the windows, exact bytes can |
| 2026-10-14 | Anomaly rule gets a two-way range estimate; caps stay raise-only | Raise-only hid the separation (×2); two-way restores it (×104) without letting a cap see an underestimate |
| 2026-10-14 | Drift-aware runtime baselines for the anomaly rule; held-out seeds pre-registered first | Flat median/p95 flagged growth and early creep (~50% FP); trend + own-residual + 1.5× gives 1.1% FP on held-out seeds |
| 2026-10-14 | Billing linked; full-history copies (no partition expiry) | Confirm the sandbox result on real dates: 0 of 9 again, allowlist unchanged; unprunable wrappers bill ~877× a pruned day filter on full `events` |
| 2026-10-15 | One rewrite round per query: an offered rewrite, when run, is not rewritten again | Catalog fixes already stop after one round; the LLM proposer rewrote its own output in a test |
| 2026-10-16 | BigQuery is the main warehouse; DuckDB stays for offline tests | Exact, free dry-run bytes; the pilot's warehouse; only BigQuery measured the rewrites (0 of 9 save bytes) |
| 2026-10-16 | Join fan-out on BigQuery estimated from the registry; blocks only unfiltered joins at ≥2× the largest input | A dry run sees bytes read, not rows produced (Q13); filtered estimates ran 100× high on Q28 (tuning set) |
| 2026-10-16 | SUM/AVG over a named identifier is blocked, with a COUNT(DISTINCT) suggestion (was a warn) | Owner's policy: ids are counted, never added. Not an automatic rewrite (a different answer); registry-only identifiers still warn (could be real amounts). Eval labels H11 (held-out) and B26 (BigQuery) relabeled warn → block with approval, recorded under `label_change`; both scores reported |
| 2026-10-16 | Column kinds by model measured, not yet wired into the Guard | 73/73 and 20/20 vs a 65/73 and 8/20 name baseline, but the prompt hinted 4 hard columns (both false-id traps); the false-id side needs a clean prompt and an independent set first |
| 2026-10-16 | Column kinds wired in frugally: triage first, a model only for edge cases, labels are proposals; ambiguous id-like names warn instead of block | Owner's direction for the e-commerce pilot: no risk, little LLM cost. TheLook: 72/73 columns settled without a model; a model or an ambiguous name can only warn, a person's confirmation blocks |
| 2026-10-16 | SUM/AVG over a confirmed CATEGORY is blocked too; events.sequence_number confirmed as category | Owner's decision: sequence_number restarts at 1 in every session (a step label, half of a session+step key), so adding or averaging it is meaningless; grouping, counting and MIN/MAX (session length) stay allowed. Only a person's confirmation blocks |
| 2026-10-16 | DuckDB backend thread-safe: one cursor per thread, locked lazy setup | A shared backend (any long-running service, e.g. an MCP server) gave 76 errors and 47 wrong verdicts in 200 checks from 8 threads; after: 0 and 0 at 8 and 32 threads. BigQuery was already safe (0/0) |
| 2026-10-16 | BigQuery check: table metadata and a column's full size cached 10 min; dry run, metadata and the join check's dry run overlap | Measured, warm, median of 8: single table 831 → 590 ms, dated join 3,101 → 613 ms (5×), same verdicts. A check now costs about one dry run. A schema change shows up within 10 minutes |
| 2026-10-16 | MCP server with `run_query` as the only warehouse path; no result rows from any tool | AI apps get the Guard in the path instead of beside it; a model cannot skip the check, and rows stay in the warehouse. Tested end to end over stdio |
| 2026-10-16 | Phoenix for observability, through OpenTelemetry, optional; JSONL stays the default | Owner's choice. Self-hosted, no vendor cost; tests and evals need no collector. One trace id across JSONL and Phoenix |
| 2026-10-16 | LLM calls traced by token counts only, not auto-instrumented | An instrumentor exports the prompt, and the prompt holds the query |
| 2026-10-17 | MCP: the deployment sets the workload class (`--workload`); a caller may only pick an equal or stricter class; no bypass through MCP | Found on a blocked 3-table join: the same query as `scheduled` became a warn and would have run. A bypass is a person's decision, not a model's |
| 2026-10-17 | Blocks carry `fix_hints`: main cause first (a table alone over the cap, a many-to-many join key, the joins' fan-out, a cartesian product), then fixes, each marked whether it changes the result; never a rewrite, never a different decision | A block listed every finding with equal weight; neither a person nor an AI could tell which change mattered. Deterministic, from the decision's own metadata: +0.7 ms on a 4.5 ms block (warm, DuckDB) |
| 2026-10-17 | Anomaly rule moved into the package and run after every completed run; flags are logged, never enforced | It was the replays' only; one copy now (dev-seed replay output identical before/after). A flag starts the report-only agent |
| 2026-10-17 | Drift trend extrapolated at most one history span past the last run (production wrapper only) | Found by test: 25 runs in 25 minutes, then a run 16 days later — the trend "expected" 8×10⁷ s and nothing could be flagged. The replays had continuous traffic, so their numbers are unchanged |
| 2026-10-17 | Investigator agent: fixed steps over the MCP tools + deterministic diagnosis; one optional Claude summary (off by default, capped per day in its own usage log, cached by facts) | Owner's choice: frugal and testable; a model-chosen tool loop costs more per investigation and is harder to test. Reports hold no SQL, literals or rule findings (two rules quote the query) |
| 2026-10-17 | Option B: flagged runs stay out of their shape's anomaly history (`customer.yaml anomaly.exclude_flagged: true`); repeated flags read as "a lasting change — a person accepts it" | Owner's choice, checked on a pre-registered online replay (held-out seeds 201–220, run once; `eval/RESULTS_replay_online.md`): catch 0.532 → 0.701, false positives 0.019 → 0.041. B catches a lasting regression to the end (creep 107/114 vs 82) but flags every recurrence of a legitimate new pattern (new_peak 40 vs 13 of 360) until a person accepts it |
| 2026-10-17 | `accept-normal <shape_id>` (CLI only): a person accepts a shape's flagged runs so far as its new normal; append-only `accepted_normal.jsonl` next to the log; later spikes still flagged | Option B flags a lasting legitimate change every run otherwise. Not through MCP: like a bypass, a person's decision. Tested: 3 flags at a new 15x level, accepted, the next run at that level not flagged, a 30 s spike after it flagged |
| 2026-10-17 | Registry corrections to the DuckDB estimate only for basic queries (one SELECT over one table: no join, CTE, subquery or UNION); joins, CTEs and subqueries keep DuckDB's own EXPLAIN estimate | Owner's call: the corrections had become hard to follow through joins and CTEs. Tuning suite 30/30 and regression 11/11 unchanged; 4 TheLook joins lose a small correction (~4% lower). Known cost: a window query wrapped in a CTE or subquery is back to the planner's ~22.6M-row overestimate (block) where alone it gets ~1.0M (allow). BigQuery's registry join fan-out estimate is unchanged (no EXPLAIN there) |
| 2026-10-17 | BigQuery is the only eval target; DuckDB stays for the unit tests. Eval split: validation = all 90 earlier queries in BigQuery SQL, relabeled for BigQuery where the guide differs (11, each with its measured reason), rerun freely; test = 40 new queries, pre-registered (`ddf0030`), run once after tuning | Owner's call. The unit tests stay on DuckDB: free, offline, ~30 s, no credentials. Split by query, not by table rows: an eval case is a query and its verdict. All 90 earlier queries had been run, so none could stay held out. First validation run: 86/90, 0 false blocks |
| 2026-10-17 | `SELECT *` judged on cost, not column count: flagged at ≥ 50% of the class cap (dry-run bytes on BigQuery, rows on DuckDB), or a median runtime ≥ 50% of the class runtime limit after ≥ 20 logged runs of the shape; the 8-column rule is gone | Owner's call: column count says nothing about cost (BigQuery bills the bytes of the columns read). Runtime has no pre-run predictor (no EXPLAIN on BigQuery, no time in a dry run), so it comes from the shape's history. TheLook dry runs: only `inventory_items` (78%) warns; `events` (367%) blocks on the cap; users 19%, order_items 13% allow. Labels changed and recorded: Q01, Q11, H07, H29, B24 warn → allow, E01 too. Tuning 30/30 (26/30 original labels); held-out rescored from stored runs, not rerun: 26/30 and 22/30 current, headlines 29/30 and 24/30 unchanged |
| 2026-10-17 | Fix hint on BigQuery blocks: a query with a LIMIT is told LIMIT doesn't reduce bytes billed, and pointed at the date filter and column list | Dry runs on TheLook: 367.2 → 367.2 MiB (`SELECT * FROM events` + `LIMIT 10`), 207.4 → 207.4 MiB with a date filter, 28.4 → 28.4 MiB with ORDER BY + `LIMIT 100`. Not suggested as a fix: it saves nothing billed and changes the result. Not on DuckDB (test-only) |
| 2026-10-17 | Report: block follow-through (fixed and ran / retried unchanged / still blocked / bypassed / nothing) | Measures whether hints help; counts runs only (checks are not logged) |
| 2026-10-17 | Bytes ceiling at the driver: every BigQuery run carries the class's `max_bytes_scanned` as `maximum_bytes_billed` (it can only lower the backend's project-wide ceiling); a bypassed block keeps only the project ceiling | Before, runs carried the project-wide 1 TB, not the 200 GB interactive cap, so a query the dry-run check got wrong could bill up to 1 TB. BigQuery refuses a job over the ceiling before reading anything (bills nothing). Seen live: H24 (runaway join) is blocked only through the bytes cap, and a dry run can't see compute. Live test: a 59.55 MiB aggregate on `events` run through `execute()` with a 10 MiB ceiling was refused (`bytesBilledLimitExceeded`, "62914560 or higher required"), 0 bytes processed and billed. A dry run does NOT enforce the ceiling (Q26, 117 MiB, accepted under 10 MiB), so the pre-run check must compare bytes itself |
| 2026-10-17 | Volume rule: warn (never block) when a query shape ran ≥ `min_runs` (20) times in `window_hours` (24) and its modeled total reaches `warn_usd_per_window` ($5), with a summary-table / materialized-view / schedule hint | Owner's priority 4: spend = cost per run × runs, and each run may be cheap (10 MiB minimum ≈ $0.00006). Pre-run evidence only (principle 8): the query log plus this run's dry run. Logged runs count billed bytes, so BigQuery's free cache hits don't inflate the total. A warn, because each query is fine on its own (priority 2). Thresholds are a judgment call until a pilot's log gives real volumes; evals don't pass a log, so scores are unchanged (validation 89/90) |
| 2026-10-17 | BigQuery rewrite gate compares BILLED bytes (`max(read, 10 MiB × tables)`), not bytes read | Owner's call (priority 4 and 5): on a small table a rewrite that reads less bills the same 10 MiB, and its sampled verification bills ~30 MiB, so offering it costs money and saves none. Live dry runs: B22 4.155 → 0.034 MiB read, B23 0.958 → 0.713, T15 0.721 → 0.058, all 10 → 10 MiB billed: no longer offered (warn). T13 39.1 → 10 MiB and T14 11.2 → 10 MiB billed: still offered. Validation 89/90 → 87/90 under current labels (B22, B23 labeled rewrite) |
| 2026-10-17 | BigQuery evals are BigQuery-native only. Validation = B01–B30 + T01–T40 (70); the DuckDB-era Q and H queries left; the old test set merged into validation; T15 rewrite → warn and T24 warn → allow recorded as label changes; guide: a rewrite must BILL fewer bytes, a sort with no LIMIT under the cap is allow | Owner's call. The 60 Q/H queries were machine-converted from DuckDB (2023 dates, DuckDB-era intents, 11 labels patched for BigQuery); almost every pattern they test recurs in B/T. Lost coverage: result-changing rewrites (Q02, Q14), to be covered by the fresh pool. The test set had fixes after its misses, so it was a tuning set anyway. Validation 68/70 current, 60/70 original labels. Next: a fresh BigQuery-native pool, pre-registered, split at random into validation and test |
| 2026-10-18 | Sampled verification includes the rewrite's short date window: when a rewrite filters a table's time-partition column to ≤ 31 days (an open "since X" counted to now), the sample is every row of that window widened by 1 day each side (up to the row limit), plus the usual block sample of the rest | T14's correct rewrite was rejected as inconclusive: a few-day window had no row in a ~10% block sample, so both sides were empty. The window read prunes to its partitions, so it adds little. Two statements (CREATE from the window, INSERT the block sample of the rest): BigQuery refuses TABLESAMPLE on a table referenced twice in one statement, measured live. Live: T14 accepted on 3 of 3 fresh samples; an off-by-one-day rewrite of it rejected (hashes differ). Validation 68/70 → 69/70 |
| 2026-10-18 | Near-unique GROUP BY: threshold 0.5 → 0.8, and a GROUP BY column is judged only with the table it comes from (resolved per SELECT scope, through derived tables and CTEs; unresolved = no table); redundant DISTINCT also covers a DISTINCT over a derived table already grouped on those columns | Owner's calls. At 0.5 a per-buyer count (`orders.user_id`, 79,570 / 125,584 = 0.63, ~1.6 orders per buyer) was a false warn (H28, T32); the guide warns on a UNIQUE key. Not 0.95: HLL counts put truly unique DuckDB columns at 0.90–0.96 (BigQuery registry 0.993–1.0); 0.8 clears the worst measured error (−17%). Before, every GROUP BY counted for every table with that column name. T21 had warned only through the 0.5 rule; its DISTINCT is now caught directly. Validation 69/70 → 70/70 (62/70 original labels) |
| 2026-10-18 | Fresh BigQuery pool pre-registered: `eval/bq_pool.yaml`, 62 queries (52 by the assistant, P01–P52; 10 by the owner, P53–P62), split by `scripts/split_bq_pool.py` (seed 20261018, stratified by label) into 31 validation + 31 test, each 12 allow / 9 warn / 7 block / 3 rewrite. Suites `bq-pool-validation` (tune freely) and `bq-pool-test` (run once) | Labels from guide v3 and data facts only (dry-run bytes, registry counts, exact join output rows from per-key counts, ~110 MiB billed; a hand-written equivalent per rewrite case); no Guard verdict seen before this commit. Expected hard cases on record: P09 (fan-out under a range filter, the guide's known exception), P11 (rare value, an even-spread estimate predicts ~333M rows, truth 1.05M). The assistant wrote both the Guard's rules and P01–P52, so they are not independent; P53–P62 are |
| 2026-10-18 | Column registry stores value counts: rows per value for every column with ≤ 1,000 distinct values (the most common 1,000 at most), one extra scan per table at build time; the join estimate uses count / rows for a listed `col = value` / `IN` value and keeps 1/ndv for any other | First pool validation run: 30/31, the one miss P11 a false block (priority 2): `country = 'Colombia'` estimated as 1/15 of users (~333M output rows), really 10 users (500,420 rows). Classified a missing feature (the registry had distinct counts, not per-value counts). Owner approved. Filled into the BigQuery registry without re-profiling (`--mode missing`, 126 MiB processed); column profiles unchanged. Found while checking: users, products, distribution_centers are views on Google's public TheLook and drift daily (Colombia 21 → 10 users), so view-based registry entries and pool facts age; pool facts remeasured, every label held. Pool validation 30/31 → 31/31 (now a tuning half); 70-query validation unchanged (70/70, 62/70 original). The test half is still unrun |
| 2026-10-18 | Registry refresh: a CLI command `cost_guard registry refresh|status`, run on demand; a scheduled refresh is a deployment REQUIREMENT (§11), not scheduled here (owner's call: "we can decide when needed"; a launchd job was written in f6a1ca4 and removed, uninstalled). Every build appends a row to `registry_runs`; backends reload the registry when its file's mtime changes (a failed read keeps the copy in memory); MCP tool `registry_status` (per-table age / stale, last run) | Owner's calls (per-table refresh ages not taken). A plain scheduler when one is chosen, not an agent or skill: the refresh is a deterministic script, and an LLM would add cost and a way to go off-script. Cost, dry-run measured: a full re-profile bills ~668 MiB ($0.004 MODELED; events 470 MiB); stale mode re-profiles only after max_age_days (35), so most runs bill 0 (measured: 0 bytes on a fresh registry). Known gap: the views (users, products, distribution_centers) drift daily but are refreshed only every 35 days |
| 2026-10-18 | MCP smoke test on BigQuery over stdio (`scripts/mcp_smoke_bigquery.py`): every tool except `run_query`, dry runs and local files only, then the project's job list checked for jobs created during the test (dry runs are not stored as jobs). 15/15, 0 jobs. Fixed: the server's `--dataset` default was `thelook_ecommerce`, which doesn't exist in the project; now `cost_guard_thelook` like the CLI | Layer 2 of the MCP checks (layer 1 = service tests, layer 3 = a real model's tool use, not built). The server runs under `customer.yaml` caps (interactive 200 GB), so P53 (367 MiB) warns there while the eval's 100 MiB cap blocks it — expected. Seen: P08's estimate is 79.8M against 208.7M real (traffic_source is skewed; join keys are still assumed even), still a block |
| 2026-10-18 | After the pool's test half (28/31, run once): (1) a view's row count comes from the registry for the large-table gate too, not only the cartesian rule; (2) a comparison inside a subquery is judged only by the subquery's own filters; (3) new rule `no_not_in_over_nulls`: warn on `x NOT IN (SELECT col FROM t …)` when the registry shows NULLs in `t.col` (unless the subquery filters `col IS NOT NULL`); no fresh entry = visible skip | Owner approved. Classified by the disagreement procedure: P32 bug (T27's fix covered the cartesian rule only), P51 bug (the wrapped-partition check descended into the NOT IN subquery), the NOT IN rule a missing feature the guide already listed — P37 and T23 had passed only through P51's false finding, so 27/31 were right for the right reason. Priority 1 (no wrong answers): one NULL makes NOT IN return nothing. P09 (fan-out under a range) stays the guide's known miss. Validation 31/31 and 70/70 (62/70 original) unchanged; the affected test cases spot-checked by dry run, the test half not rerun (it is now a tuning set; headline stays 28/31) |
| 2026-10-18 | LLM model: `claude-opus-5-5` → `claude-haiku-5-5` (llm.py MODEL). No Gemini client, no Batch API | Owner's call: "keep it simple and use the haiku". The LLM is optional and fenced (rewrite proposals still verified, explanations, investigator summary; never decides), so the cheapest model fits. Compared: Haiku 5.5 $0.10 / $0.50 per 1M tokens (prompts ≤ 100k) vs Gemini 2.5 Flash-Lite $0.10 / $0.40 — about $0.0003 vs $0.00027 per Guard call; Gemini's free tier would let Google use the prompts (they hold the query's SQL), and it needed a new client. Batch API (50% off, replies up to 24 h) skipped: the Guard's calls are interactive and the offline volume is tiny. A one-call check: Haiku accepts the same options (effort, structured output, server-side fallbacks). Earlier LLM results were measured with Opus and are not re-measured |
| 2026-10-18 | Evals viewable in a local Phoenix: `run_eval.py --phoenix` (one trace per case, root span `eval <id>` with pattern / expected / got / ok, the Guard's spans under it; project `cost-guard-evals`, numbers only) and `--phoenix-content` (adds the SQL, the verdict and the model's prompt and reply as OpenInference input/output, to EXPORTED spans only; refused unless the eval is on TheLook; project `cost-guard-evals-content`; resource attribute `cost_guard.content = included`). Phoenix in its own venv (`.venv-phoenix`), served on 127.0.0.1 | Owner's call: "keep both" — readable traces for evals, the privacy rule intact for real traffic (MCP, CLI, JSONL never carry content). Measured on the pool's validation half: 31 traces / 514 spans per project; SQL present on the 31 content-project root spans only; none of the 21 literals of those queries found in the numbers-only project or the JSONL trace. Phoenix's default host is 0.0.0.0 (reachable from the network), so it is started with PHOENIX_HOST=127.0.0.1 |
| 2026-10-18 | Phoenix shows cost: LLM spans carry OpenInference token counts (`llm.token_count.*`, `llm.provider`, `llm.model_name`) and `llm.cost.total` (MODELED at Haiku 5.5's $0.10 / $0.50 per 1M); each eval case carries `eval.bytes_estimate`, `eval.bytes_billed_if_run` (10 MiB-per-table minimum) and `eval.usd_if_run_modeled`. First Haiku eval on the pool's validation half: 31/31 unchanged, 23 calls, $0.0039 MODELED, ~1.9 s per call; 13 of 16 explanations state the real reason; 0 of 9 rewrite proposals offered, correctly ([`eval/RESULTS_bq_pool_validation_llm_haiku.md`](eval/RESULTS_bq_pool_validation_llm_haiku.md)) | Owner asked why Phoenix showed no cost: those runs made no LLM calls, the token attributes had non-standard names, and BigQuery cost is not an LLM cost. Numbers only, so both Phoenix modes carry them. Found by the eval, NOT fixed yet (owner to decide): the rewrite prompt says "DuckDB dialect" (P25's proposal used an interval BigQuery rejects) and the explainer gets findings only, not a cap block's reason (P01, P08 unexplained; P03 misses the cap) |
| 2026-10-18 | LLM prompt fixes: the rewrite prompt names the dialect in use (`sql_ast.current_dialect()`; on BigQuery with the two measured rules — no MONTH/YEAR interval on TIMESTAMP, TIMESTAMP literals for TIMESTAMP columns); the explainer gets `guard.explain_inputs()` — decision and reason first, then findings the reason doesn't state, then up to three fix hints — and runs for cap blocks without findings too | Owner approved. Both found by the first Haiku eval. Rerun on the pool's validation half: explanations stating the real reason 13/16 → 16/16; invalid BigQuery proposals 1 → 0; LLM rewrites offered 0/9 both times (correct); cost $0.0039 → $0.0050 MODELED, ~1.9 → ~3.5 s per call (longer inputs and answers) |
| 2026-10-18 | Join fan-out under a range filter (P09): `t.col BETWEEN a AND b` / `>`, `>=`, `<`, `<=` on a NUMERIC column whose value counts are complete (they add up to the column's non-NULL rows) keeps the exact share of rows in the range; several conditions on one column are combined into one range first. Any other range (too many values to store, dates, strings) and LIKE stay traced only | Owner approved ("go for 2"). Chosen over min/max interpolation: an even spread over the range can be far off on skewed data, and acting on a guess risks false blocks (priority 2) — the reason ranges were left out after Q28. P09: users.age has 59 values, complete; 3,398 users aged 30–31 → 169.9M estimated, 169.9M real → block. Validation 31/31 and 70/70 unchanged. The labeling guide's exception (a range is only traced) now applies only to ranges without complete value counts; the registered pool header keeps its v3 text |
| 2026-10-18 | Spend budgets (`budgets.py`): per user, per team (members in `customer.yaml`) and per workload class (scheduled jobs: their own budget, not counted toward users or teams). Period: calendar month, UTC. Before a run: spent (the query log's bytes billed × price, else the ledger's model; blocked runs spend nothing) + this query (dry-run bytes, 10 MiB-per-table minimum). Warn at 80%, block over 100%; the stricter scope decides. Approval at the hard limit = the existing recorded bypass, which for a budget-only block keeps the class's bytes ceiling. Identity: the CLI's `--user`, the MCP server's `--user` (`COST_GUARD_USER`); a caller can't pass another id; without one, user and team budgets are skipped visibly. Reports: the CLI report lists every budget; MCP `workload_report` only the server's own user's scopes | Owner's call: "go with your suggestion" (calendar month, user and team, 80% / 100%, recorded approval, scheduled jobs on their own budget). Decided before the run, like every other check (principle 8). Known limit: two queries checked at once see the same spend, so a limit can be overshot by about one query, bounded by the per-query caps. Starting limits ($50 per user, $500 analytics team, $1,000 scheduled) are the fictional pilot's, MODELED; untested against real spend (TheLook's largest query bills ~0.5 GiB ≈ $0.003). LLM spend not counted (~$0.0003 per call) |
| 2026-10-18 | Owner test set pre-registered: `eval/bq_owner_test.yaml`, 12 queries written by the owner (O01–O12), suite `bq-owner-test` (run once). Table names pointed from bigquery-public-data.thelook_ecommerce to the cost_guard_thelook copies; `original_sql` kept. Labels: 11 warn, 1 allow | The first set not written by the assistant who wrote the rules. Facts: dry runs, the registry, O03's exact join output (278,241 rows, many-to-many under the cap), O06's status values ('cancelled' matches nothing: a wrong answer the guide does not cover). Lopsided toward one rule (missing partition filter); its value is the 9 key joins that must not be blocked. No Guard verdict seen before this commit |
| 2026-10-18 | LLM suggestions: with `--llm`, a warned or cost-blocked query gets one model call (`llm.suggest_improvement`, replacing the rewrite proposer when available) for an improved query + every change + whether the result changes. Claimed unchanged → tried as a rewrite through the existing gates (fewer bytes billed, sampled equality). Otherwise, or if those fail, attached as `suggestion` after a read-only check and a dry run (valid; bytes read / billed and MODELED $ against the original). Never run or swapped in; the decision stands. Runtime is the model's reasoning only (not measurable before a run). Capped (`llm.suggestions_per_day`, 200, per process) and cached per exact query + findings (memory only) | Owner's call: "I want llm to improve the query if it is warned… optimized based on runtime or cost", with result-changing improvements allowed when labeled. Priority 1 kept: nothing changes the user's query without them choosing it. Owner test with Haiku (a rerun after the scored 12/12): 9 of 11 warns got a suggestion, all valid; it fixed mistakes the Guard can't see (O03/O05/O06/O11 double count, O03 join key, O06 'cancelled'); only O05/O06 bill less on TheLook (30 → 20 MiB); $0.0107 MODELED for 22 calls. Cap and cache are per process: a multi-process deployment shares neither |
| 2026-10-18 | One backend rule for every entry point (`config.choose_backend`): `--backend`, else `--db` alone means DuckDB, else `COST_GUARD_BACKEND`, else `customer.yaml` `warehouse.backend: bigquery` (project from GCP_PROJECT, dataset cost_guard_thelook). Used by the CLI (check, run, investigate-anomalies, registry), the MCP server and `build_column_registry.py`. `run_eval.py --suite` is required. The investigator agent parses in the backend's dialect. The unused `bigquery:` stretch-goal section of `customer.yaml` removed | Owner asked to audit for DuckDB used where BigQuery is meant. Found: CLI check/run were DuckDB-only; the MCP server defaulted to DuckDB; the investigator parsed BigQuery fingerprints in DuckDB's dialect in its own thread (backticked table names failed, so investigations lost table info); `warehouse.backend` was never read. Live check: `cost_guard check` with no flags now dry-runs on BigQuery |
| 2026-10-18 | Security, enforced: `secret_scan.py` finds known credential formats (Anthropic / OpenAI / Google API keys, AWS keys, GitHub / Slack tokens, JWTs, private-key blocks) and the exact values of this process's `*_KEY / *_TOKEN / *_SECRET / *_PASSWORD` variables (.env). A query holding one is blocked FIRST — no dry run, no model call, no fix hints, no bypass; every MCP tool response and error is redacted; the query log's fingerprint and exported content traces are redacted. MCP server instructions are generated at startup from the server's own setup (warehouse, project/dataset, tables with partition columns, caps, user, budget) with the workflow in order and the security rules; one MCP prompt `analyze_with_cost_guard`; tool descriptions say when to use each | Owner's call: the calling model should know the right order and tools, and must not share API keys or other secrets from the user or the developer. Instructions guide; the server enforces (blocked queries never run, no bypass, fixed user and class, caps, budgets, secret block, redaction), so a model that ignores them still can't overspend or leak. No generic "password = ..." pattern: `WHERE token = 'abc'` is ordinary SQL |
| 2026-10-18 | Layer-3 agent eval (`eval/run_agent_eval.py`): Haiku with the MCP server's instructions and tools, live on BigQuery, 10 tasks, scored from its tool calls (run without check, ran a block, retried unchanged, silent result-changing swap, > 3 blocked revisions, secret echoed). First run stopped at A04 and found: (1) a bug — `execute` downloaded every result row to count them (`list(job.result())`); now `max_results=0` and BigQuery's `total_rows`; (2) a gap — a self-join fan-out with `a.id != b.id` is "filtered", so only traced, and a ~208M-row query was allowed and ran (estimate 79.8M). Fix (2) NOT made: the owner's call | The test's purpose was this kind of finding. A01–A03 ran normally before A04. Results of the first run were not saved (written only at the end; per-task lines now flush). Rerun pending |
| 2026-10-18 | Join fan-out: `a.col != b.col` between two relations, where col is near-unique (≥ 0.95) on one side and not a key of their join, removes at most the smaller side's rows; the estimate subtracts that and is acted on. Any other inequality between relations still marks it filtered (traced only) | Owner approved ("do 1 and 2 first"). Found by the agent eval: Haiku wrote "every OTHER user of the same traffic source" with `a.id != b.id`, which was traced only, and a ~208M-row join ran. Now: 79.8M estimated → block (the estimate runs low on skewed traffic_source; real ~208M). Validation 31/31 and 70/70 unchanged |
| 2026-10-18 | Agent eval rerun after the two fixes: 10/10 tasks clean, 0 violations ([`eval/RESULTS_agent_eval.md`](eval/RESULTS_agent_eval.md)); $0.0134 MODELED agent tokens, 60 MiB billed | Haiku followed the generated instructions: check before run, no blocked query run or retried, blocks explained, NOT EXISTS instead of NOT IN, result-changing changes disclosed, DELETE and both secret probes refused. Open product question for the owner: `run_query` returns no rows, so an AI app cannot answer data questions through Cost Guard |
| 2026-10-18 | Scope: the pre-run verdict only ("ready to fly or not": CLI `check`, MCP `check_query`). No user UI, no result rows, no plain-English-to-SQL. The Streamlit demo (`demo/app.py`, synthetic DuckDB, DuckDB-era presets) and its smoke test removed; streamlit dropped from requirements | Owner's call. `demo/demo.py` and `demo/warehouse.py` stay (the CLI demo and the tests' synthetic warehouse) |
| 2026-10-18 | MCP stop rule for an agent's retry loop enforced in the server: after 3 blocked `check_query` / `run_query` results in a row, the 3rd carries `stop` and both tools refuse for 5 min (other tools keep working); any non-block resets the count (`customer.yaml` `mcp`). Investigator cooldown per shape 60 → 15 min | Owner asked before a demo. The "stop after 3" rule was only an instruction; an agent that ignored it could call `check_query` without end. Changes no verdict. 443 tests pass; MCP smoke on BigQuery 15/15 |
| 2026-10-20 | Apache Superset integration via `SQL_QUERY_MUTATOR`: a BLOCK stops the SQL Lab / chart query with Cost Guard's reason and hints; other verdicts pass unchanged (as `cost_guard run`); the check runs as a subprocess in Cost Guard's venv | Owner asked. Superset 6.1 pins sqlglot 28 and other versions, so one process can't hold both. Fails open: no verdict = not stopped (BigQuery's own errors and `maximum_bytes_billed` remain). Setup needed `rich`, `cachetools` and `flask-caching==2.3.1` beyond `pip install apache-superset` (6.1.0 as installed 2026-10-20) |
| 2026-10-19 | Filter value case: a filter value that matches one stored value only when case is ignored is offered as a REWRITE with the stored spelling (`value_case.py`, from the registry's complete value counts); two stored values that differ only by case WARN. The stored spelling, not `LOWER(col)` | Owner's call (chose REWRITE over WARN and over `LOWER()`). Changes the result on purpose (the original matches no row with that value), so it is the one rewrite without sample verification, marked `rewrite_source` `value_case` and "the result changes". Changes verdicts: on the run-once owner set O06 (`NOT IN ('cancelled', 'Returned')`, labeled warn) would now get rewrite; its recorded 12/12 stands for the code that ran and the set is not rerun. No other eval query is affected (all sets scanned offline against the BigQuery registry) |
| 2026-10-16 | Rewrite verification: 2 s limit for long-running services, results remembered (passed and failed) in memory for 24 h; dry runs of identical SQL reused for 10 min | Live, one process: first check of a rewritable query 4.0 s (answer without the rewrite, verification continues), the same query again 0.01 s with the verified rewrite. Failed verifications are not re-run (cost). Nothing written to disk; the CLI and evals wait, so their results stay deterministic |
| 2026-10-23 | Project renamed Cost Guard → Query Preflight: package `query_preflight`, CLI `python -m query_preflight`, MCP server `query-preflight`, `superset/query_preflight_hook.py`, env vars `QUERY_PREFLIGHT_*` (the log key renamed in `.env`, value kept, so fingerprints are unchanged), Phoenix projects `query-preflight-evals[-content]` | Owner's call: "Cost Guard" made people expect a dollar-savings figure, but the job is the pre-run verdict ("ready to fly or not"), cost being one reason among wrong results and runaway joins. "Query", not "SQL": leaves room for other query languages without claiming them; the evidence is SQL warehouses only (BigQuery, DuckDB). Unchanged on purpose: the BigQuery dataset `cost_guard_thelook` (a real GCP resource), committed RESULTS / PREREG files, pre-registered sets, earlier decision-log rows, and the generic "cost guard" wording inside LLM prompts (changing a prompt would change measured LLM behavior). No verdict or label changed; 457 tests pass |
| 2026-10-23 | Eval reproducibility: every eval runner records the code version, taken before the run (`eval/code_version.py`: commit + uncommitted changes to src/, eval/, scripts/, customer.yaml, requirements.txt), in its results JSON and summary header. The raw JSON of the run-once sets (holdout, bigquery first run + rerun, bq-test, bq-pool-test, bq-owner-test + its LLM rerun) archived in `eval/archive/` with sha256 and the commit that recorded each summary | From an outside review's checklist. `eval/results/` is gitignored, so the raw output behind every held-out headline lived on one laptop; a paper reviewer could not re-score it. The archived runs predate the version field: their code version is "the tree just before the recording commit", stated as such. Re-scoring the archived holdout gives the recorded 29/30 |
| 2026-10-23 | Threshold judge records how each call ended (`judge_outcome`: judged / refused / no output (stop reason) / error / no judge) and its MODELED cost (`judge_usd`); an API error now holds instead of raising. `run_judge_eval.py` reports, in order: bad proposals wrongly approved (must be 0), subtle-bad rejected or held, good rejected or held (false alarms), then how many holds were not judgments, and $ per decision; each row shows the judge's reason next to the signal it should cite | From the same review: a refusal or error also holds, which scores as correct on a bad row, so an all-holds run could look perfect. Scoring unchanged (reject or hold is correct on every bad row). Tested with fakes; the live Claude run (14 calls, cents) is still to be run once and recorded as-is |
| 2026-10-23 | Judge eval: a second number, `2b_subtle_bad_right_verdict` — reject on S1, S2, S4, S5, S6 (clear evidence: tail, drift, threshold at p95, tail with a short span), reject or hold on S3 (one-hour history: the judge prompt allows either); only outcome `judged` counts. The safety number (reject or hold both correct, bad approved must be 0) stays the headline; the judge prompt is unchanged | Owner's call (option B, over keeping safety only (A) and strict-only (C)), set BEFORE the first live run, so it is a pre-registered rule, not tuning. Hold and reject do the same thing in production today (no shadow trial), so safety-only scoring cannot tell a judge that holds on everything from one that understands; strict-only would score a safe judge as failing. Checked with a fake judge that holds on every subtle row: safety 6/6, right verdict 1/6 |
| 2026-10-23 | First live threshold-judge run (Haiku 5.5, 14 calls, $0.0043 MODELED, commit `ef58b9b`): 0/6 bad approved, 6/6 subtle-bad right verdict, 8/8 good proposals rejected, 0 refusals or errors ([`eval/RESULTS_judge.md`](eval/RESULTS_judge.md)). Recorded as-is; nothing tuned | Owner's go. Cause from the judge's own reasons: every proposal follows a regression (recent median ≥ 1.5× baseline, `REGRESSION_RATIO`), the prompt never explains `recent_median_sec`, so the judge treats the regression as the new normal and rejects. Classified as missing context plus an open design question for the owner: is a 5-run regression a runaway to stop or the new normal to allow? The judge stays optional and is not wired into `decide()`, so nothing in production changes. A fix now makes these 20 proposals a tuning set |
| 2026-10-23 | **Verdict first, model second** (principle). `guard.decide()` takes no model and is deterministic; the model's help moves to `guard.assist()`, which runs only on warn and block, after the verdict, and fills `suggestion` / `explanation` / `assist_notes` only (an assert checks decision and reason are unchanged). A model rewrite that passes the rewrite gates (shared `_vet_rewrite`: read-only, cheaper, sampled equality) is now an OFFER on a warn (`suggestion.verified_rewrite`), no longer a `rewrite` decision; its shape is logged (`rewrite_fingerprint`) so it gets one round. CLI `check --llm`: the verdict line first, then an `assist` line; MCP: `check_query` returns at once with `assist: {status: pending, assist_id}`, new tool `get_assistance`; `run_query`: decide, run, then assist. Evals measure the verdict's latency and checks before the model's step | Owner's call: "verdict always must be the first" and the model serves the queries the verdict failed; architecture option "all 3" (test first, then the split, then this row). Before, the verdict waited for the model (~1.9–3.5 s per call measured with Haiku) and a verified model rewrite could turn a warn into `rewrite` inside the verdict step (never happened in the measured runs: 0 model rewrites offered). `tests/test_verdict_first.py`: `decide` has no model parameter; on allow / warn / rewrite / block the model changes nothing and is not called on allow or rewrite; MCP returns the verdict while the model is still blocked; the CLI prints the verdict first. Default runs (no `--llm`) are unchanged |
| 2026-10-24 | Judge prompt, option 1: a proposal follows a slowdown (the latest runs' median ≥ 1.5× the usual, `investigator.REGRESSION_RATIO`, pinned by a test) that the threshold is meant to stop, so recent runs above it are the intended effect; every other statistic describes the normal runs before the slowdown, and the threshold is judged against those. The reject / hold criteria are unchanged. `run_judge_eval.py` never overwrites the first live run: a later live run needs `--rerun` and writes `RESULTS_judge_rerun.md` | Owner's call (option 1 over "may be the new normal" and "drop recent runs from the payload"): it matches how the Investigator works, and the baseline trend ratio already covers data growth. Only the missing context was added: listing the eval's own warning signs in the prompt would teach to the test. The 20 proposals are a tuning set from here; the first run (0/6 bad approved, 8/8 false alarms) stays the first measurement |
| 2026-10-24 | Judge rerun after option 1 (tuning set, Haiku 5.5, 14 calls, $0.0037 MODELED, commit `15821e3`): 0/6 bad approved, 6/6 subtle-bad right verdict, 0/8 false alarms, 0 refusals ([`eval/RESULTS_judge_rerun.md`](eval/RESULTS_judge_rerun.md)) | Every good-row reason now judges the threshold against the pre-slowdown baseline and names the recent median as the intended slowdown; each subtle-bad reason cites its own signal. A tuning number: the fix followed the first run on the same 20 proposals, written by the same author as the prompt. Next: a fresh, pre-registered proposal set, run once |
| 2026-10-24 | Semantic hints (`semantic_hints.yaml`, `semantic_hints.py`): per table its grain and key, the valid joins (many-side = one-side key), column notes. Two warn-only rules: a join between hinted tables on an undeclared column pair; SUM/AVG over a one-side column with its many side joined in the same SELECT (fan-out). Test set `eval/hints_test.yaml` pre-registered in this commit (26 queries: 8 wrong keys, 8 fan-out sums, 10 correct, some look-alikes), scored by `eval/run_hints_eval.py`, run once | Owner's call. Wrong answers are priority 1, and the owner test showed 4 of 12 queries double counting (`sale_price * num_of_item`) and one wrong join key, caught only by the optional model. Measured on the local copy: the double count reports $20.6M for $10.8M (1.9×), the user_id join 2.04× the rows; every key unique, every declared join 0 orphans. On the tuning sets the rules fire on 3 of 131 queries, all already blocked (B18, B19, T06: events × users on state/city), 0 false alarms; the fan-out rule never fires there, so its evidence is the owner test, which cannot score it (known cases) — hence the fresh set. Same author as the rules: queries written as an analyst would, including forms expected to be missed |
| 2026-10-24 | Semantic-hints rules ON, as warnings: `decide()` reads `customer.yaml semantic_hints` (path to the file; absent = off, silently; unreadable = a visible skip), cached until the file changes. Test set result first ([`eval/RESULTS_hints_test.md`](eval/RESULTS_hints_test.md)): 22/26 — 7/8 wrong keys, 6/8 fan-out sums, 1/10 false alarms | Owner's call ("enable both rules"). Warn, never block: the one false alarm (cost of goods sold: a product's cost once per item sold is the intent) shows a one-side value over many-side rows is sometimes right. Known misses, not fixed (a fix would make the set a tuning set): `USING` joins; a fan-out produced in a CTE or subquery and summed outside it. Tuning sets rerun with the rules on: TheLook 30, bq-validation 70, bq-pool-validation 31 — no verdict changed; findings only on B18, B19, T06, already blocked |
| 2026-10-24 | Threshold-judge TEST set pre-registered: `eval/judge_test.yaml`, 14 proposals (8 good, 6 bad) of kinds not in the tuning set, labels, right verdicts and signals fixed before any judge saw them; `run_judge_eval.py --set test` runs it once (`RESULTS_judge_test.md`; a later run needs `--rerun`) | Owner's call, F-B2 (a step up: data doubled three weeks ago, 50% headroom left) kept as reject — the "may be the new normal" case option 1 leaves to the trend ratio. Good look-alikes (two modes far below the threshold, a step down) catch a judge that rejects anything unusual. Construction checked on numbers only: every proposal passes the gates; two fixes before the lock (the Investigator rule rounded up in the test set so a 25% move stays 25%; F-G5's noise lowered so its trend ratio is not a fake 1.20). Same author as the judge prompt: a known limit |
| 2026-10-24 | Threshold-judge test set, first and only run (Haiku 5.5, 14 calls, $0.0044 MODELED, commit `d4b36d3`): 14/14 — 0/6 bad approved, 6/6 right verdict (F-B3 hold), 0/8 false alarms, 0 refusals ([`eval/RESULTS_judge_test.md`](eval/RESULTS_judge_test.md)) | The first measurement on proposals the judge was not tuned on; every reason cites the right evidence, including the look-alikes (F-G5 two modes, F-G8 a step down). Limits stated with it: synthetic, 14 proposals, the prompt's author wrote them; real proposals need a real query log. The judge stays optional, after the run, behind the gates and the shadow trial |
| 2026-10-24 | Cost scorecard (`eval/cost_scorecard.py`, `eval/SCORECARD_cost.md`): each BigQuery set's stored headline run (run-once sets from `eval/archive/`, tuning sets their latest run) restated per single run in billed bytes and MODELED dollars, next to its exact score: bad stopped (upper bound) / flagged / rewritten / missed, share of bad bytes caught, verified-rewrite savings, false blocks and warns on good queries (counts; analyst time only with two ASSUMPTION flags), the Guard's verification cost (~30 MiB each, measured). Bytes from dry runs made at scorecard time (free). `run_eval.py` now stores each case's `bytes_estimate`, `bytes_billed_if_run`, `usd_if_run_modeled` | Planned before the rename ("dollars next to the verdict scores, never replacing them"). First result: the held-out sets catch 96–100% of bad queries' bytes (bigquery 99%, bq-test 96%, bq-pool-test 98%, owner test 100%); missed bad bytes 10–45 MiB per set. Verified rewrites save 30–157 MiB per run while their verification bills ~30 MiB each, so a rewrite pays back after one or two runs (priority 5, the Guard's own cost). Dollars are tiny on TheLook; the shares carry the meaning |
| 2026-10-29 | Superset stops a value-case miss: a verdict with `value_case_fixes` (a filter value that matches a stored value only when case is ignored, `value_case.py`) raises in the `SQL_QUERY_MUTATOR` hook with "did you mean", the registry note and the corrected query; nothing is swapped in. Ambiguous case twins (a warn) and cost-only rewrites still pass unchanged. The verdict itself is unchanged (still `rewrite`); only Superset's handling of it changed | Owner's choice of the recommended option (alternatives: swap in silently; leave as is). As written, the query returns no rows for that value, and an empty SQL Lab result or chart reads as "there are none"; swapping it in silently would change a query the analyst didn't see. Checked on BigQuery with a free dry run: `status = 'cancelled'` stopped ('Cancelled', 18,786 rows); `'Cancelled'` passed |
| 2026-10-29 | Registry refresh scheduled MONTHLY (launchd, the 1st at 09:00, `scripts/schedule/`), shipped with `DRY_RUN = True`: the job runs `registry refresh --mode all --dry-run` (new flag: which tables the mode would profile and why; reads the catalog metadata and the local registry; scans, writes and bills nothing) and appends to `runs/registry_refresh.log`. `DRY_RUN = False` makes it a real refresh | Owner's call (monthly, dry run until they switch it). Recommended was weekly; monthly is safe only with `--mode all` (a monthly `stale` run leaves entries past `max_age_days` 35 for up to ~25 days), so the job uses `all`: ~610 MiB billed a run when real (measured 2026-10-23, ~$0.004 MODELED). Dry run checked on BigQuery: would profile all 7 tables, 0 bytes |

## 13. Open questions

- Registry distinct counts (HyperLogLog) vs exact on TheLook: 0.800 to 1.164
  over all 73 columns (e.g. events.browser: 5 values, counted as 4).
  `plan_estimate.HLL_MARGIN` (1.05) is narrower than that. Pre-registered
  check (`eval/RESULTS_hll_margin.md`): the GROUP BY bound is below the exact
  count on 19 of 73 columns at 1.05 and 2 at 1.20; exact counts fix it and
  take 4.9x longer to build (0.29 s to 1.43 s), above the rule's 2x. No
  verdict on the tuning or regression suites moves under any option. The
  rule does not choose an option, so nothing is changed; the owner decides
  between exact counts (cost) and a margin (still not a bound). The join
  recompute uses the margin in the unsafe direction (51 of 73 columns above
  exact); a lower bound (distinct / 1.2) fixes that but is very loose for
  small counts.
  Since registry corrections now apply only to basic queries, this affects
  single-table GROUP BYs and window queries only.
- Many users: see section 14 (daily budgets, threshold saving, log reads).
- Resolved: the rewrite catalog emits BigQuery SQL on BigQuery (dialect per
  decision, real TIMESTAMP literals; 3a6ced9, b98a68c). Found by the
  BigQuery eval and fixed after it (its first-run 24/30 stands): the
  filter-after-join rule is off on BigQuery (a9cab30), and GROUP BY columns
  keep their table (98a8c64).
- Rewrite verification on BigQuery runs real queries on TEMP-table samples:
  ~30 MiB billed (BigQuery's 10 MiB-per-table minimum) and ~8–10 s. A
  long-running service now caps the wait (guard.rewrite_wait_sec: 2) and
  remembers results in memory; the one-shot CLI still waits. Open: the
  memory is per process — several service processes each verify once.
- The BigQuery join fan-out estimate applies filters on the partition
  column (as a dry-run share of partitions read) and stays out of decisions
  when any other filter is present; the watchdog's timeout is the backstop.
  It assumes evenly spread join keys, so on skewed keys it runs LOW: a
  users-events join on `city` for one month estimated 221k rows, really
  12.6M (57×). That only ever misses a block, but it misses often. Most
  common values per column in the registry would fix most of it.
- Count rows read instead of rows out of scans? It would make "scanned"
  honest, but it shifts every estimate and possibly verdicts.
- Are rewrites worth offering on DuckDB, where most measure ~1× (pushdown
  measured ×0.92 on Q08)?
- Resolved: a NOT IN-over-NULLs rule exists (`no_not_in_over_nulls`, built
  2026-10-18 after the pool's test half, 6b57e48): warn on
  `x NOT IN (SELECT col FROM t …)` when the registry shows NULLs in `t.col`,
  unless the subquery filters `col IS NOT NULL`; no fresh entry = a visible
  skip (decision log, 2026-10-18).
- Clustering-aware rules, and a layout advisor that mines the query log for
  partition/cluster suggestions.
- When do the evals move to the new caps? That needs a recorded relabel of
  Q03, Q10, E06 and E10 (and the held-out guide's 5M cap).

## 14. Recommendations for future improvements

Not built; ordered by value for an e-commerce pilot on BigQuery. Each keeps
the design rules above: deterministic decisions, no LLM on the query path,
nothing that can block wrongly without a person's confirmation.

0. **Shadow mode over the customer's query history — the first step of a
   pilot.** Read the last N days from `INFORMATION_SCHEMA.JOBS` (read-only),
   run each query through the Guard with dry runs only (free, nothing
   executes, no model), and report what it would have blocked, warned or
   rewritten, with bytes and modeled dollars and the top query shapes. Not
   built yet: this project's own history (61 jobs in 30 days, 1 user, all
   made by its tooling — registry scans, copies, verification sessions) has
   no analyst workload to measure, so a run here would show nothing; it
   needs a real customer's history.
1. **Daily budgets per user and per team.** Caps are per query, so many
   analysts each under the cap can still run up a large bill. Approach:
   `customer.yaml` budgets (bytes or modeled dollars per user / team per
   day); the Guard sums today's `bytes_estimate` from the query log for the
   caller and warns at ~80 %, blocks past 100 % (bypassable with a reason,
   traced). Needs an indexed or rolling log (item 3) and a trusted caller
   identity from the deployment.
2. **Shared state for a multi-process service.** Learned thresholds are
   saved last-writer-wins; the verification memory is per process. Approach:
   one writer (or file locks) for the threshold store; a small shared cache
   (or the warehouse itself) for verification results.
3. **Query-log index.** Every `run` reads the whole log (offered rewrites);
   cost grows with users. Approach: an index by fingerprint, or a rolling
   window of recent rewrites kept in memory.
4. **Most common values per column in the registry.** Join estimates
   assume evenly spread keys and ran 57× low on a `city` join. Approach:
   top-k values and their shares per column at registry build time; use
   them for the skewed part of a join. Partly done 2026-10-18: value counts
   (columns with ≤ 1,000 distinct values) now set the share a `col = value`
   filter keeps; join KEYS are still assumed evenly spread.
5. **MCP server and a report-only investigator agent**, if the project is
   to be agentic: Query Preflight's tools (check, run through the Guard, table
   info, history, report) for AI apps, with `run` the only path to the
   warehouse and no raw rows returned; an agent triggered only when a query
   shape breaks its own history, writing a report, changing nothing.
   Groundwork done: thread-safe backends, fast cached checks. MCP server
   BUILT (2026-10-16, `mcp_server.py`, with Phoenix tracing); the report-only
   agent BUILT 2026-10-17 (`investigator_agent.py`, triggered by `anomaly.py`).
6. **Independent evaluation.** Every query set so far was written by the
   assistant that wrote the rules. A set of real or hand-written queries
   (and the tricky columns for the column-kind check) from someone else,
   pre-registered and run once.
7. **Sort check on BigQuery.** A dry run has no plan; an AST-only warning
   for ORDER BY without LIMIT over a large, barely filtered table. Low value
   on on-demand pricing (no extra bytes; runtime is capped by the watchdog).
8. **Portability check on a time-billed warehouse** (e.g. Snowflake), when a
   pilot customer uses one: cost there is runtime × warehouse size, so the
   Guard's estimate would be a runtime proxy.

