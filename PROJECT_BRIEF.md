# Project brief: Cost Guard + Investigator agents

## Addendum (2026-10-11): local-first decision

Primary backend is now **local DuckDB** with the TheLook CSV export from Kaggle
(free Kaggle account, zero cloud spend). BigQuery moves to stretch goal. Cost is
modeled from EXPLAIN row counts + measured runtime; every dollar figure stays
labeled as modeled. The Guard/Investigator logic is unchanged — only the warehouse
adapter differs, which is what the swappable interfaces are for. Everything else in
this brief stands.

## Addendum (2026-10-13): scope decision on agentic upgrades

Adopted: (4) evals + tracing — `eval/run_eval.py` (labeled cases, accuracy /
catch rate / false-positive metrics) and dependency-free JSONL tracing on every
Guard decision; (3) Investigator memory + workplan — local JSONL episodic memory
of past incidents per fingerprint plus a multi-step `investigate()` (enumerate →
regression check → memory recall → proposals with evidence).

Deferred explicitly: (1) tool-calling ReAct Guard loop — the hot path stays
deterministic until the eval harness shows the static rules missing real cases;
(2) proposer–critic rewrite verification — the deterministic sampled-hash check
suffices while the rewrite catalog is small. Revisit when the evidence says so.

Handoff notes for a fresh coding session. Everything below was decided in an earlier planning chat; the CLI agent has no memory of it, so treat this file as the source of truth. Put it in the repo root (or rename to CLAUDE.md).

## One-line pitch
Two AI agents that control analytics query cost on a shared platform: a Guard that checks queries before they run, and an Investigator that learns from query history to tighten the Guard's rules automatically.

## Business story
We are an analytics technology company that hosts an analytics platform for customers across industries. First pilot customer: an e-commerce company. Analysts write queries but cannot see infrastructure cost; the platform sees cost but does not write the queries. Runaway queries hurt platform margin (flat pricing) or the customer's bill (usage pricing). Persona to anchor demos on: an analytics lead at the e-commerce customer saying "our revenue dashboard takes 8 minutes and costs keep rising, I don't know which queries are the problem."

## Scope (one week, part-time, so keep it small)
- Batch warehouse queries only. Streaming is explicitly out of scope (roadmap line in the README only).
- Single agent per role, about 5 shared tools. No multi-agent framework.
- Fully automated: no human approval workflow. A blocked query can be bypassed only by a logged config flag (escape hatch for demos).
- POC with ONE mock customer (see "Mock customer" below). No multi-tenancy and no multi-environment packaging. State plainly in the README that the customer is mocked.

## Mock customer
The pilot customer is mocked: a fictional e-commerce company whose warehouse is the TheLook dataset in our own BigQuery project (the "customer environment"). Define it in a small `customer.yaml`: name, workload classes (interactive dashboards vs scheduled reports), hard caps for bytes and runtime per class, and a price-per-TB assumption for modeled dollar figures.

Deployment boundary (design for it, do not build it): real customers will want to run everything inside their own private environment. So keep these three things separable and swappable behind small interfaces: (1) the static rules package, (2) the LLM client (customer-hosted model, redacted hosted model, or no LLM with deterministic rewrites only), (3) the telemetry export (aggregates only, opt-in, never SQL text). Result comparison must return only a pass/fail and hash, never rows. In the README, describe this as the roadmap from POC to private deployment.

## Data
- Primary: BigQuery public dataset `bigquery-public-data.thelook_ecommerce` (TheLook). Dry-run gives exact bytes scanned without running anything.
- Public tables are probably not partitioned. Create partitioned copies in your own project (DDL is in the header of `eval_queries.yaml`).
- Fallback if no cloud account: DuckDB with TPC-H at a large scale factor; use `EXPLAIN` and scanned rows as a cost proxy.
- Set a bytes-billed cap and a timeout on the BigQuery project before running anything.
- Real query log: BigQuery's `INFORMATION_SCHEMA.JOBS` records bytes billed and runtime for jobs run in your own project. Capture that from the eval runs, then generate additional synthetic history scaled from those real measurements.

## Architecture
Loop: Guard (before execution) -> warehouse -> query log -> Investigator (after execution) -> threshold updater -> rule store -> Guard.

Shared tools (deterministic code, not LLM judgment):
1. `estimate_cost`: dry-run or `EXPLAIN`, returns bytes and a runtime prediction
2. `get_table_metadata`: size, partitioning, clustering
3. `run_sample_query`: read-only, capped
4. `compare_results`: equivalence check (hash or row-level diff, float tolerance)
5. `get_query_history`: per-fingerprint stats from the log (Investigator mainly)

Guard decisions: allow / warn / rewrite / block. The gate is mostly deterministic (cost estimate plus rules). The LLM runs only when a query is flagged, to explain why and propose a cheaper rewrite. A rewrite is accepted only if `compare_results` passes on a sample.

## Rules
Layer 1, static benchmark rules (cold start): no `SELECT *` on wide tables; partitioned tables need a partition filter; no cross join without a join condition; no function wrapped around a partition column in a filter; filter before join; repeated identical subqueries become CTEs; hard cap on bytes scanned; hard cap on wall-clock time, with different caps per workload class (interactive vs scheduled).

Layer 2, adaptive thresholds (learned from history):
- Fingerprint queries by normalizing literals.
- Per-fingerprint baselines: median and p95 of bytes scanned AND duration.
- Anomaly rule: warn when a query is far above its own history (above p95 or a few times the median). Regression rule for runtime.
- Cold start: only static rules until a fingerprint has about 20 runs.
- Cost rule for runtime: weight duration by compute rate (warehouse size) where relevant.

Guardrails against the "ratchet" problem (history full of bad queries teaching the system that expensive is normal):
- Absolute hard caps that never adapt; learned thresholds must sit below them.
- Bound how fast a threshold can move per update.
- Tighten only on evidence, for example when a verified-equivalent cheaper rewrite exists.
- Shadow mode first: log "would have blocked" before enforcing a new threshold.
- Auto-rollback if a new threshold's block rate jumps past a set limit.

Runtime enforcement has three points: before the run (predicted runtime), during the run (statement timeout or watchdog; prefer the warehouse's built-in timeout), after the run (log actual duration). Only auto-kill read-only queries. Runtime is noisy (caching, concurrency), so use percentiles with a minimum run count and report relative numbers in the DuckDB demo.

## Eval
- `eval_queries.yaml`: 15 bad/good pairs (30 queries) with pattern labels, `expected_effect`, `rewrite_equivalent`, and `guard_action_expected`. NOT yet run, and table/column names were written from memory: verify against the live TheLook schema first.
- Some pairs intentionally change the result (Q02, Q14): the Guard should warn or ask, not auto-rewrite.
- BigQuery's optimizer may already fix some patterns (Q04, Q05, Q15). Measure before claiming savings; "no savings, pattern still detected" is a valid result.
- Q13 is a deliberately runaway join to test the long-running thresholds. Set caps before running it.
- Adaptive-threshold demo is an offline replay: split the log by time (70% history, 30% test), compute baselines on the first part, replay the second through the Guard. Compare static-only vs static plus adaptive on catch rate and false-positive rate.
- Metrics to report: bytes-scanned reduction, runtime reduction, rewrite equivalence rate (target 100%), diagnosis accuracy, share of runaway queries caught before or during execution, tool calls per investigation.
- Known limitation to state: no human ground truth for false positives. Proxy signals (user resubmits a modified query, same fingerprint blocked repeatedly) are noisy.

## Suggested build order
1. Verify schema, create partitioned copies, run the 30 queries with dry-run first, log bytes/runtime/equivalence (this is the real query history).
2. Implement the five tools plus the fingerprinting and baseline code.
3. Guard: static rules plus decision logic, CLI only, run against the eval set.
4. Investigator loop and threshold updater with shadow mode and rollback; offline replay.
5. Thin API with streaming plus a small front end (chat, findings card with diff, per-tenant cost/runtime/blocks table). If time is short, use Streamlit or Gradio instead of React; cut UI before cutting the eval.
6. Docker, deployed demo, README (problem, architecture, results table, trade-offs, limitations, roadmap incl. streaming).

## Open items for the human
- Which GCP project and budget cap to use.
- Whether to build the front end in React or Streamlit.
- Any real number from existing cost-estimation work worth citing (do not invent business numbers; label any dollar figures as modeled from a stated price per TB).
