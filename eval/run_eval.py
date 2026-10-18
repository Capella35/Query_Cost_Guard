#!/usr/bin/env python3
"""Eval harness for the Guard.

Default suite — the headline: the 30 labeled TheLook queries in
eval/eval_queries.yaml (15 bad/good pairs) run through guard.decide() against
the real TheLook data in DuckDB (data/thelook.duckdb). Reports, per pattern,
expected vs actual action, with every miss listed. Labels were fixed before
the Guard was run on these queries.

Metrics:
- exact accuracy: decision == guard_action_expected,
- catch rate: bad queries that got any non-allow verdict,
- false-positive rate: good queries that got any non-allow verdict,
- rewrites offered: for each, equivalence is re-checked on the FULL data
  (both queries executed, canonical result hashes compared) and runtime
  savings are measured (median of 3 runs). This is the "rewrite equivalence
  rate" — the sampled check in the Guard is necessary, not sufficient.

--suite bigquery runs the PRE-REGISTERED BigQuery set (eval/bq_queries.yaml)
against the TheLook copies in BigQuery (--project; dataset cost_guard_thelook)
under eval/eval_caps_bigquery.yaml. Dry runs, plus small capped sample queries
when a rewrite is verified; run once, like holdout.

BigQuery is the target (2026-10-17); DuckDB stays for the unit tests only.
--suite bq-validation: the BigQuery-native queries (B01-B30, T01-T40),
labeled for BigQuery (eval/bq_validation.yaml, scripts/build_bq_validation.py)
— the set to tune on, rerun freely. --suite bq-test: the first pre-registered
test set (eval/bq_test.yaml), run once 2026-10-17 and since merged into
validation; kept as the record of that run.
--suite bq-pool-validation / bq-pool-test: the fresh pool (eval/bq_pool.yaml,
pre-registered 2026-10-18), split by scripts/split_bq_pool.py. Tune on the
validation half; the test half is run ONCE.

--suite regression runs the 11 inline cases on the synthetic warehouse
(also covered by tests/test_guard_regression.py). Those were written next
to the rules: 100% there is expected, not evidence.

--llm claude enables the Claude rewrite proposer + explainer (F3.1/F3.3;
needs Anthropic credentials). Default: no LLM.

Results: eval/results/<suite>_<timestamp>.json, and for the TheLook suite a
committed summary at eval/RESULTS_thelook.md. Traces append to
runs/traces.jsonl.

Run:  .venv/bin/python eval/run_eval.py [--suite thelook|regression] [--llm claude]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "demo"))
sys.path.insert(0, str(ROOT / "eval"))

from cost_guard import sql_ast  # noqa: E402
from cost_guard.guard import decide  # noqa: E402
from cost_guard.results import result_hash  # noqa: E402
from cost_guard.tracing import Tracer  # noqa: E402

FLAGGED = ("warn", "block", "rewrite")
FULL_RUN_TIMEOUT_SEC = 60


SUITE_FILES = {
    "thelook": ("eval_queries.yaml", "RESULTS_thelook.md", "30 TheLook queries"),
    "holdout": ("holdout_queries.yaml", "RESULTS_holdout.md", "held-out TheLook queries"),
    "bigquery": ("bq_queries.yaml", "RESULTS_bigquery.md",
                 "pre-registered BigQuery TheLook queries"),
    "bq-validation": ("bq_validation.yaml", "RESULTS_bq_validation.md",
                      "BigQuery validation set (BigQuery-native B and T queries)"),
    "bq-test": ("bq_test.yaml", "RESULTS_bq_test.md", "pre-registered BigQuery test set"),
    "bq-pool-validation": ("bq_pool.yaml", "RESULTS_bq_pool_validation.md",
                           "BigQuery pool, validation half"),
    "bq-pool-test": ("bq_pool.yaml", "RESULTS_bq_pool_test.md",
                     "BigQuery pool, test half (pre-registered)"),
}
SUITE_SPLIT = {"bq-pool-validation": "validation", "bq-pool-test": "test"}  # one file, two halves
RUN_ONCE = ("holdout", "bigquery", "bq-test", "bq-pool-test")  # held-out / pre-registered: first run is the result
BQ_SUITES = ("bigquery", "bq-validation", "bq-test", "bq-pool-validation", "bq-pool-test")  # BigQuery dry runs, eval_caps_bigquery.yaml


def load_thelook_cases(filename: str = "eval_queries.yaml", split: str | None = None) -> list[dict]:
    data = yaml.safe_load((ROOT / "eval" / filename).read_text())
    return [
        {
            "id": q["id"],
            "sql": q["sql"],
            "expected": q["guard_action_expected"],
            # Label before any recorded post-hoc change (label_change in the YAML).
            "expected_original": (q.get("label_change") or {}).get("from", q["guard_action_expected"]),
            "label_change": q.get("label_change"),
            "pattern": q["pattern"],
            "role": q["role"],
            "pair": q.get("pair"),  # only eval_queries.yaml is built in pairs
            "why": q["description"],
        }
        for q in data["queries"]
        if split is None or q["split"] == split
    ]


def load_suite_cases(suite: str) -> list[dict]:
    return load_thelook_cases(SUITE_FILES[suite][0], SUITE_SPLIT.get(suite))


def load_regression_cases() -> list[dict]:
    from regression_cases import CASES

    return [
        {**c, "pattern": "regression", "role": "good" if c["expected"] == "allow" else "bad",
         "expected_original": c["expected"]}
        for c in CASES
    ]


def full_check(wh, original: str, rewrite: str) -> dict:
    """Execute both on the full data: equivalence + measured runtime."""
    con = wh._connect()
    ordered = sql_ast.has_order_by(sql_ast.parse_one(original))
    hashes, times = [], []
    for sql in (original, rewrite):
        runs = []
        rows = None
        for _ in range(3):
            res = wh.execute(sql, timeout_sec=FULL_RUN_TIMEOUT_SEC)
            if res.status != "completed":
                return {"equivalent_full": None, "detail": f"full run {res.status}"}
            runs.append(res.duration_sec)
        rows = con.execute(sql).fetchall()
        hashes.append(result_hash(rows, ordered=ordered))
        times.append(statistics.median(runs))
    return {
        "equivalent_full": hashes[0] == hashes[1],
        "original_sec": round(times[0], 4),
        "rewrite_sec": round(times[1], 4),
        "speedup": round(times[0] / times[1], 2) if times[1] else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["thelook", "holdout", "bigquery", "bq-validation", "bq-test",
                             "bq-pool-validation", "bq-pool-test", "regression"],
                    default="thelook")
    ap.add_argument("--rerun", action="store_true",
                    help="holdout/bigquery: run again although it has been run (recorded as a rerun)")
    ap.add_argument("--project", help="bigquery suite: GCP project holding the TheLook copies")
    ap.add_argument("--dataset", default="cost_guard_thelook")
    ap.add_argument("--db", default=str(ROOT / "data" / "thelook.duckdb"))
    ap.add_argument("--llm", choices=["none", "claude"], default="none")
    ap.add_argument("--rescore", metavar="RESULTS_JSON",
                    help="re-score a STORED run's verdicts against the current labels "
                         "(after recorded label changes); runs no query and no Guard")
    args = ap.parse_args()
    if args.rescore:
        print(json.dumps(rescore(Path(args.rescore), args.suite), indent=2))
        return

    rerun = False
    if args.suite in RUN_ONCE and (ROOT / "eval" / SUITE_FILES[args.suite][1]).exists():
        if not args.rerun:
            raise SystemExit(
                f"the {args.suite} set has already been run (eval/{SUITE_FILES[args.suite][1]} "
                "exists). It is meant to be run ONCE: re-running after changing the Guard turns "
                "it into a tuning set. Pass --rerun to run anyway; the summary will say it is a rerun."
            )
        rerun = True
    if args.suite in BQ_SUITES:
        from cost_guard.backends.bigquery_backend import BigQueryWarehouse

        if not args.project:
            raise SystemExit("--suite bigquery needs --project")
        wh = BigQueryWarehouse(args.project, args.dataset, maximum_bytes_billed=20 * 1024**3)
        cases = load_suite_cases(args.suite)
    elif args.suite in SUITE_FILES:
        from cost_guard.backends.duckdb_backend import DuckDBWarehouse

        if not Path(args.db).exists():
            raise SystemExit(f"{args.db} missing — run scripts/load_thelook_duckdb.py first")
        wh = DuckDBWarehouse(args.db)
        cases = load_suite_cases(args.suite)
    else:
        from warehouse import build_synthetic_warehouse

        print("building synthetic warehouse...")
        wh = build_synthetic_warehouse("/tmp/cg_eval.duckdb")
        cases = load_regression_cases()

    llm = None
    if args.llm == "claude":
        from cost_guard.llm import ClaudeLLM

        llm = ClaudeLLM(backend=wh)

    from eval_config import load_eval_customer

    customer = (load_eval_customer(ROOT / "eval" / "eval_caps_bigquery.yaml")
                if args.suite in BQ_SUITES else load_eval_customer())  # caps the labels were written under
    tracer = Tracer()
    results = []
    for case in cases:
        v = decide(case["sql"], customer=customer, backend=wh, tracer=tracer, llm=llm)
        spans = [s for s in tracer.spans if s["name"] != "guard.decide"]
        r = {
            "id": case["id"],
            "pattern": case["pattern"],
            "role": case["role"],
            "expected": case["expected"],
            "expected_original": case.get("expected_original", case["expected"]),
            "got": v.decision,
            "ok": v.decision == case["expected"],
            "ok_original_labels": v.decision == case.get("expected_original", case["expected"]),
            "label_change": case.get("label_change"),
            "latency_ms": round(sum(s["t_ms"] or 0 for s in spans), 1),
            "checks": len(spans),
            "estimate_rows": v.estimated_rows,
            "estimate_source": v.estimate_source,
            "findings": v.findings,
            "skipped_rules": v.skipped_rules,
            "explanation": v.explanation,
        }
        if v.decision == "rewrite":
            r["rewrite_source"] = v.rewrite_source
            r["rewritten_sql"] = v.rewritten_sql
            r["verification"] = v.verification
            if args.suite in SUITE_FILES and args.suite not in BQ_SUITES:  # DuckDB: execute both
                r.update(full_check(wh, case["sql"], v.rewritten_sql))
        results.append(r)
        tracer.save(ROOT / "runs" / "traces.jsonl")

    bad = [r for r in results if r["role"] == "bad"]
    good = [r for r in results if r["role"] == "good"]
    rewrites = [r for r in results if r["got"] == "rewrite"]
    checked = [r for r in rewrites if r.get("equivalent_full") is not None]
    metrics = {
        "n": len(results),
        "exact_accuracy": round(sum(r["ok"] for r in results) / len(results), 3),
        "exact_accuracy_original_labels": round(
            sum(r["ok_original_labels"] for r in results) / len(results), 3),
        "labels_changed": sum(r["expected"] != r["expected_original"] for r in results),
        "catch_rate": round(sum(r["got"] in FLAGGED for r in bad) / len(bad), 3) if bad else None,
        "false_positive_rate": round(sum(r["got"] != "allow" for r in good) / len(good), 3) if good else None,
        # Not all errors cost the same: a blocked good query stops someone's
        # work (and breeds bypasses); a warned one costs a glance.
        "false_block_rate": round(sum(r["got"] == "block" for r in good) / len(good), 3) if good else None,
        "false_warn_rate": round(sum(r["got"] in FLAGGED and r["got"] != "block" for r in good) / len(good), 3)
        if good else None,
        "under_block_rate": (round(sum(r["got"] != "block" for r in results if r["expected"] == "block")
                                   / sum(r["expected"] == "block" for r in results), 3)
                             if any(r["expected"] == "block" for r in results) else None),
        "rewrites_expected": sum(r["expected"] == "rewrite" for r in results),
        "rewrites_offered": len(rewrites),
        "rewrite_equivalence_rate_full_data": (
            round(sum(r["equivalent_full"] for r in checked) / len(checked), 3) if checked else None
        ),
        "avg_checks_per_decision": round(sum(r["checks"] for r in results) / len(results), 1),
        "llm": args.llm,
        "llm_calls": getattr(llm, "calls", 0),
    }

    by_pattern: "OrderedDict[str, dict]" = OrderedDict()
    for r in results:
        p = by_pattern.setdefault(r["pattern"], {"n": 0, "exact": 0, "flagged": 0, "ids": []})
        p["n"] += 1
        p["exact"] += r["ok"]
        p["flagged"] += r["got"] in FLAGGED
        p["ids"].append(f"{r['id']}:{r['expected']}->{r['got']}")

    print(f"\n{'pattern':<36}{'n':>3}{'exact':>7}{'flagged':>9}  cases (expected->got)")
    for name, p in by_pattern.items():
        print(f"{name:<36}{p['n']:>3}{p['exact']:>7}{p['flagged']:>9}  {', '.join(p['ids'])}")
    misses = [r for r in results if not r["ok"]]
    print(f"\nmisses ({len(misses)}):")
    for r in misses:
        why = "; ".join(r["findings"])[:110] or "no finding"
        print(f"  {r['id']} {r['pattern']}: expected {r['expected']}, got {r['got']} — {why}")
    for r in rewrites:
        print(f"\nrewrite {r['id']} ({r['rewrite_source']}): full-data equivalent="
              f"{r.get('equivalent_full')} speedup={r.get('speedup')} "
              f"({r.get('original_sec')}s -> {r.get('rewrite_sec')}s)")
    print("\n" + " | ".join(f"{k} {v}" for k, v in metrics.items()))

    ts = datetime.now(timezone.utc)
    out = {"ts": ts.isoformat(), "suite": args.suite, "metrics": metrics,
           "by_pattern": by_pattern, "cases": results}
    dest = ROOT / "eval" / "results" / f"{args.suite}_{ts:%Y%m%d_%H%M%S}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, default=str))
    print(f"results -> {dest.relative_to(ROOT)}")
    if args.suite in SUITE_FILES:
        where = (f"`{args.project}.{args.dataset}` (BigQuery dry runs; caps "
                 "`eval/eval_caps_bigquery.yaml`)" if args.suite in BQ_SUITES
                 else "`data/thelook.duckdb` (Kaggle TheLook CSVs)")
        write_summary(ts, metrics, by_pattern, results, args.suite, rerun, where)


def _scores(verdicts: dict, cases: list[dict], label_key: str, role_of) -> dict:
    n = len(cases)
    bad = [c for c in cases if role_of(c) == "bad"]
    good = [c for c in cases if role_of(c) == "good"]
    blocks = [c for c in cases if c[label_key] == "block"]
    return {
        "exact": f"{sum(verdicts[c['id']] == c[label_key] for c in cases)}/{n}",
        "bad_flagged": f"{sum(verdicts[c['id']] in FLAGGED for c in bad)}/{len(bad)}",
        "good_flagged": f"{sum(verdicts[c['id']] != 'allow' for c in good)}/{len(good)}",
        "good_blocked": f"{sum(verdicts[c['id']] == 'block' for c in good)}/{len(good)}",
        "blocks_missed": f"{sum(verdicts[c['id']] != 'block' for c in blocks)}/{len(blocks)}",
    }


def rescore(results_json: Path, suite: str) -> dict:
    """A stored run re-scored under the original and the current labels —
    the verdicts are the ones that run produced; nothing is executed."""
    run = json.loads(results_json.read_text())
    verdicts = {r["id"]: r["got"] for r in run["cases"]}
    cases = load_suite_cases(suite)
    changed = [c for c in cases if c["label_change"]]

    def original_role(c):
        return (c["label_change"] or {}).get("from_role", c["role"])

    return {
        "run": results_json.name,
        "original_labels": _scores(verdicts, cases, "expected_original", original_role),
        "current_labels": _scores(verdicts, cases, "expected", lambda c: c["role"]),
        "changed": {c["id"]: f"{c['expected_original']} -> {c['expected']} (got {verdicts[c['id']]})"
                    for c in changed},
    }


def write_summary(ts, metrics, by_pattern, results, suite="thelook", rerun=False,
                  where="`data/thelook.duckdb` (Kaggle TheLook CSVs)") -> None:
    _, out_name, title = SUITE_FILES[suite]
    lines = [
        f"# Guard eval: {title}",
        "",
        f"Generated by `eval/run_eval.py --suite {suite}` on {ts:%Y-%m-%d %H:%M} UTC against "
        f"{where}, LLM: "
        f"`{metrics['llm']}`. Original labels were set before the Guard was run on them; "
        "any later change is listed under Label changes.",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in metrics.items()]
    lines += ["", "## By pattern", "", "| pattern | n | exact | flagged | cases (expected → got) |",
              "|---|---|---|---|---|"]
    for name, p in by_pattern.items():
        lines.append(f"| {name} | {p['n']} | {p['exact']} | {p['flagged']} | "
                     f"{', '.join(i.replace('->', ' → ') for i in p['ids'])} |")
    changed = [c for c in results if c["expected"] != c["expected_original"]]
    if changed:
        lines += ["", "## Label changes (made after seeing results)", "",
                  f"Exact accuracy: **{sum(r['ok_original_labels'] for r in results)}/{len(results)}** "
                  f"with the original labels, **{sum(r['ok'] for r in results)}/{len(results)}** with the "
                  "current ones. Each change is recorded under `label_change` in "
                  f"`eval/{SUITE_FILES[suite][0]}`.", "",
                  "| query | original → current | Guard | reason |", "|---|---|---|---|"]
        for r in changed:
            reason = (r.get("label_change") or {}).get("reason", "")
            lines.append(f"| {r['id']} | {r['expected_original']} → {r['expected']} | {r['got']} | {reason} |")
    lines += ["", "## Misses (current labels)", ""]
    for r in results:
        if not r["ok"]:
            why = "; ".join(r["findings"]) or "no rule fired"
            lines.append(f"- **{r['id']}** ({r['pattern']}): expected `{r['expected']}`, "
                         f"got `{r['got']}` — {why}")
    rw = [r for r in results if r["got"] == "rewrite"]
    if rw:
        lines += ["", "## Rewrites offered", ""]
        for r in rw:
            lines.append(
                f"- **{r['id']}** ({r['rewrite_source']}): full-data equivalent = "
                f"{r.get('equivalent_full')}, {r.get('original_sec')}s → {r.get('rewrite_sec')}s "
                f"(×{r.get('speedup')}); sampled check: {r['verification']}"
            )
    if rerun:
        lines.insert(2, "**RERUN** — this set had been run before; this result is NOT a "
                        "first, held-out measurement.\n")
    if rerun:  # never overwrite the first, held-out result
        out_name = out_name.replace(".md", "_rerun.md")
    (ROOT / "eval" / out_name).write_text("\n".join(lines) + "\n")
    print(f"summary -> eval/{out_name}")


if __name__ == "__main__":
    main()
