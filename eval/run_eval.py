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


def load_thelook_cases() -> list[dict]:
    data = yaml.safe_load((ROOT / "eval" / "eval_queries.yaml").read_text())
    return [
        {
            "id": q["id"],
            "sql": q["sql"],
            "expected": q["guard_action_expected"],
            "pattern": q["pattern"],
            "role": q["role"],
            "pair": q["pair"],
            "why": q["description"],
        }
        for q in data["queries"]
    ]


def load_regression_cases() -> list[dict]:
    from regression_cases import CASES

    return [
        {**c, "pattern": "regression", "role": "good" if c["expected"] == "allow" else "bad"}
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
    ap.add_argument("--suite", choices=["thelook", "regression"], default="thelook")
    ap.add_argument("--db", default=str(ROOT / "data" / "thelook.duckdb"))
    ap.add_argument("--llm", choices=["none", "claude"], default="none")
    args = ap.parse_args()

    if args.suite == "thelook":
        from cost_guard.backends.duckdb_backend import DuckDBWarehouse

        if not Path(args.db).exists():
            raise SystemExit(f"{args.db} missing — run scripts/load_thelook_duckdb.py first")
        wh = DuckDBWarehouse(args.db)
        cases = load_thelook_cases()
    else:
        from warehouse import build_synthetic_warehouse

        print("building synthetic warehouse...")
        wh = build_synthetic_warehouse("/tmp/cg_eval.duckdb")
        cases = load_regression_cases()

    llm = None
    if args.llm == "claude":
        from cost_guard.llm import ClaudeLLM

        llm = ClaudeLLM(backend=wh)

    tracer = Tracer()
    results = []
    for case in cases:
        v = decide(case["sql"], backend=wh, tracer=tracer, llm=llm)
        spans = [s for s in tracer.spans if s["name"] != "guard.decide"]
        r = {
            "id": case["id"],
            "pattern": case["pattern"],
            "role": case["role"],
            "expected": case["expected"],
            "got": v.decision,
            "ok": v.decision == case["expected"],
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
            if args.suite == "thelook":
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
        "catch_rate": round(sum(r["got"] in FLAGGED for r in bad) / len(bad), 3) if bad else None,
        "false_positive_rate": round(sum(r["got"] != "allow" for r in good) / len(good), 3) if good else None,
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
    if args.suite == "thelook":
        write_summary(ts, metrics, by_pattern, results)


def write_summary(ts, metrics, by_pattern, results) -> None:
    lines = [
        "# Guard eval: 30 TheLook queries",
        "",
        f"Generated by `eval/run_eval.py` on {ts:%Y-%m-%d %H:%M} UTC against "
        "`data/thelook.duckdb` (Kaggle TheLook CSVs), LLM: "
        f"`{metrics['llm']}`. Labels were set before the Guard was run.",
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
    lines += ["", "## Misses", ""]
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
    (ROOT / "eval" / "RESULTS_thelook.md").write_text("\n".join(lines) + "\n")
    print("summary -> eval/RESULTS_thelook.md")


if __name__ == "__main__":
    main()
