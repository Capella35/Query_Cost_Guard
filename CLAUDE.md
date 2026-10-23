# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Query Preflight checks warehouse SQL before it runs (allow / warn / rewrite / block),
runs allowed queries under hard limits, logs them, and learns from the log.
**BigQuery is the target warehouse**; DuckDB is used for the unit tests and the
offline demo. `DESIGN.md` is the source of truth for decisions (newer than
`PROJECT_BRIEF.md`); `README.md`'s Status table says what is built.

## Commands

```bash
.venv/bin/python -m pytest -q                                   # all tests (~30 s, offline, no credentials)
.venv/bin/python -m pytest -q tests/test_join_estimate.py::test_name   # one test
# The CLI, MCP server and registry build open customer.yaml warehouse.backend
# (BigQuery) unless --backend is given; --db FILE alone means DuckDB (tests, demo).
PYTHONPATH=src .venv/bin/python -m query_preflight check "SELECT ..."       # verdict only, nothing runs (a free dry run)
PYTHONPATH=src .venv/bin/python -m query_preflight run "SELECT ..."         # guard + watchdog + query log (BILLED on BigQuery)
PYTHONPATH=src .venv/bin/python -m query_preflight report                   # workload report from the log

# BigQuery evals (GCP_PROJECT is in .env; the project is agentic-project1-511621,
# dataset cost_guard_thelook). Dry runs are free; rewrite verification bills ~30 MiB.
.venv/bin/python scripts/build_bq_validation.py --project $GCP_PROJECT   # regenerate eval/bq_validation.yaml
.venv/bin/python eval/run_eval.py --suite bq-validation --project $GCP_PROJECT
.venv/bin/python eval/run_eval.py --rescore eval/results/<run>.json      # rescore a stored run, no warehouse
.venv/bin/python scripts/mcp_smoke_bigquery.py      # MCP server over stdio on BigQuery, free (dry runs, 0 jobs)
PHOENIX_HOST=127.0.0.1 .venv-phoenix/bin/phoenix serve   # local Phoenix UI on :6006 (own venv)
.venv/bin/python eval/run_eval.py --suite bq-validation --project $GCP_PROJECT --phoenix [--phoenix-content]

# Column registry (REQUIRED to be refreshed on a schedule in a deployment; not scheduled here — run on demand)
PYTHONPATH=src .venv/bin/python -m query_preflight registry status --backend bigquery    # local file, free
PYTHONPATH=src .venv/bin/python -m query_preflight registry refresh --backend bigquery   # --mode stale (default) | missing | all
```

There is no linter or build step. Tests import `src/` and `demo/` via
`tests/conftest.py`; the `synthetic_wh` fixture builds a synthetic DuckDB
warehouse, and `thelook_wh` tests skip when `data/thelook.duckdb` is absent.
BigQuery is faked in unit tests (`FakeClient` in `tests/test_bigquery_backend.py`).
`--llm claude` on the CLI, MCP server or evals turns on the model; the default
makes no model calls. `.env` holds `ANTHROPIC_API_KEY` and `GCP_PROJECT`: never print it.

## Architecture

Three points in time (`DESIGN.md` §3):

1. **Before the run: `guard.decide()`** (`src/query_preflight/guard.py`). Parses SQL
   with sqlglot (`sql_ast.py`, the dialect is switched per backend with
   `using_dialect`); runs the read-only check; gets the estimate from
   `backend.estimate()` (BigQuery dry-run bytes, DuckDB EXPLAIN rows corrected
   by `plan_estimate.py`); fetches table metadata and the **column registry**
   (`column_registry.py`: pre-built distinct counts, row counts and per-value
   counts, refreshed with `query_preflight registry refresh`, never scanned per
   query; backends reload it when the file changes); runs `rules/static_rules.py` plus the join fan-out
   estimate (`join_estimate.py`, rows from the registry since a dry run has no
   row counts); compares against the workload class caps; then tries rewrites
   (`rewrites.py` deterministic catalog only: `decide()` takes no model). A
   rewrite is offered only after `tools/compare_results` returns the
   same rows on sampled inputs and, on BigQuery, it bills fewer bytes
   (`runtime.bigquery_billed`: at least 10 MiB per table). Blocks get
   `fix_hints.py`. Every step is a span in `tracing.py`. **Verdict first:**
   the model's help comes after, from `guard.assist()` (warn and block only;
   explanation, improved query, a verified model rewrite as an OFFER), and
   never changes the decision (`tests/test_verdict_first.py`).
2. **During the run: `runtime.run_query()`.** Executes only non-blocked queries,
   with the class's runtime cap (or a learned lower limit) as `job_timeout_ms`
   and its bytes cap as `maximum_bytes_billed`. It reads the query log first,
   so history-based rules (SELECT * runtime, the volume rule, spend budgets in
   `budgets.py`) get their input from there. Budgets are charged to the
   deployment's user (MCP `--user`), never to a caller-supplied id. Appends a literal-free record to `runs/query_log.jsonl`.
3. **After the run:** `anomaly.py` compares the run with its shape's history;
   `investigator_agent.py` writes report-only investigations; `thresholds.py`
   holds learned limits, which can only tighten below the hard caps, through
   gates, shadow mode and auto-rollback. These are not yet wired into `decide()`.

`backends/base.py` is the backend protocol (`bigquery_backend.py`,
`duckdb_backend.py`). `mcp_server.py` exposes the same pipeline as MCP tools.
`llm.py` holds every model call. `customer.yaml` is the fictional pilot
customer's policy: caps per workload class, cost model, volume rule, registry
settings.

## Rules for working here

- **Policy is the owner's call.** Anything that changes what blocks or warns
  needs the user's approval. Follow DESIGN.md §2 "Policy priorities" (no wrong
  answers > no false blocks > catch runaways pre-run > spend at volume > the
  Guard's own cost) and "When the Guard and the policy disagree": find the cause
  in the trace first, then classify it as bug / guide wrong / deliberate
  difference / missing feature.
- **Decide before the query runs.** Verdicts come from dry runs, the registry
  and the query log. The timeout and bytes ceiling are backstops. "BigQuery
  bills nothing for it" is not a reason to allow.
- **Eval integrity.**
  - Labels are changed only openly: `label_from` / `label_change` with the
    measured reason, in a separate commit, with both scores reported.
  - The BigQuery evals are BigQuery-native only: validation = B01–B30 +
    T01–T40. `eval/bq_pool.yaml` (P01–P62, pre-registered 2026-10-18) is
    split by `scripts/split_bq_pool.py` into `bq-pool-validation` (tune on
    it) and `bq-pool-test` (run once). `eval/bq_test.yaml` (pre-registered
    `ddf0030`, run once, 35/40) and the other run-once sets (`holdout`,
    `bigquery`, `bq-pool-test`) are never rerun silently. `run_eval.py`
    refuses without `--rerun`, and a fix made after seeing a set's misses
    makes it a tuning set.
  - `eval/bq_validation.yaml` is generated: edit `RELABELS` (labels set at
    build time), `POST_RUN_RELABELS` (changes after a run, scored both ways) or
    `HEADER` in `scripts/build_bq_validation.py`, not the YAML. Its header holds the
    labeling guide.
  - Evals use frozen caps (`eval/eval_caps*.yaml`), not `customer.yaml`.
- **LLM is frugal and fenced.** Deterministic first; the model only handles edge
  cases, preferably offline, capped and cached. Its output may explain or
  propose, never block anything unconfirmed.
- **Record measurements.** A rule made warehouse-aware cites the dry run or live
  run that justified it (see `BIGQUERY_PRUNES_THROUGH` in `static_rules.py`).
  Significant choices get a dated row in the DESIGN.md decision log (§12).
  Dollar figures are labeled MODELED.
- **Privacy.** Logs and traces hold query fingerprints and numbers, never SQL
  text, literals or result rows. The one exception is opt-in: `run_eval.py
  --phoenix-content`, TheLook evals only, exported spans only (own Phoenix
  project); never add content to the JSONL trace, the MCP server or the CLI.
- **Git.** Stage files by explicit path. `docs/architecture-v2.html` is the
  user's working copy: never commit it. `eval/results/` and `data/` are
  gitignored; the `eval/RESULTS_*.md` summaries are committed.
