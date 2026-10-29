#!/usr/bin/env python3
"""MCP smoke test, end to end over stdio, BigQuery backend. Free: only tools
that dry-run or read local files (check_query, get_table_info,
get_query_history, workload_report, registry_status); run_query is never
called. Afterwards it lists the project's BigQuery jobs created during the
test (an API call, free): dry runs are not stored as jobs, so the count must
be 0 — nothing ran and nothing was billed.

Run:  .venv/bin/python scripts/mcp_smoke_bigquery.py [--project $GCP_PROJECT]
Exit 0 when every check passes; prints one line per check.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

TOOLS = {"check_query", "run_query", "get_table_info", "get_query_history",
         "workload_report", "registry_status", "get_assistance"}
# (name, sql, expected decision) — from the pool's validation half, every one
# dry-run only (no rewrite case, so no verification sample). The server runs
# under customer.yaml's caps (interactive: 200 GB), NOT the evals' frozen
# 100 MiB, so a query the pool blocks for bytes only warns here.
CASES = [
    ("P62 allow", "SELECT event_type, COUNT(*) AS cnt FROM events WHERE created_at >= "
                  "TIMESTAMP '2026-10-10 00:00:00+00' GROUP BY event_type", "allow"),
    ("P55 warn", "SELECT ip_address, COUNT(*) AS cnt FROM events GROUP BY ip_address", "warn"),
    ("P53 warn (367 MiB < 200 GB cap)", "SELECT * FROM events", "warn"),
    ("P08 block (fan-out)", "SELECT a.id, b.id AS same_source_user FROM users a JOIN users b "
                            "ON a.traffic_source = b.traffic_source WHERE a.country = 'Spain'", "block"),
    ("P11 allow (value counts)", "SELECT a.id, b.id AS same_gender_user FROM users a JOIN users b "
                                 "ON a.gender = b.gender WHERE a.country = 'Colombia'", "allow"),
    ("non-read-only block", "DELETE FROM orders WHERE status = 'Cancelled'", "block"),
]


def _data(res):
    return getattr(res, "structured_content", None) or getattr(res, "structuredContent", None) or {}


async def smoke(project: str, dataset: str, tmp: Path) -> list[tuple[str, bool, str]]:
    from mcp import ClientSession, StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "query_preflight.mcp_server", "--backend", "bigquery", "--project", project,
              "--dataset", dataset, "--log", str(tmp / "log.jsonl"),
              "--traces", str(tmp / "traces.jsonl"), "--no-investigations"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, cwd=str(ROOT))
    out: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            check("lists the seven tools", set(tools) == TOOLS, ", ".join(sorted(tools)))
            ro = {n for n, t in tools.items() if t.annotations and t.annotations.read_only_hint}
            check("only run_query is not read-only", ro == TOOLS - {"run_query"}, ", ".join(sorted(ro)))
            fp = None
            for name, sql, want in CASES:
                res = await s.call_tool("check_query", {"sql": sql})
                d = _data(res)
                fp = fp or d.get("fingerprint")
                extra = f"{d.get('decision')}: {str(d.get('reason'))[:90]}"
                check(f"check_query {name}", not res.is_error and d.get("decision") == want
                      and d.get("trace_id"), extra)
                if want == "block" and "fan-out" in name:
                    check("  block carries fix_hints", d.get("fix_hints"), f"{len(d.get('fix_hints') or [])} hints")
            res = await s.call_tool("check_query", {"sql": CASES[0][1], "workload_class": "scheduled"})
            check("looser workload class refused", res.is_error,
                  str(res.content[0].text if res.content else "")[:90])
            d = _data(await s.call_tool("get_table_info", {"table": "events"}))
            check("get_table_info events", d.get("partitioned_by") == "created_at"
                  and d.get("num_rows") == 2_425_610,
                  f"partitioned by {d.get('partitioned_by')}, {d.get('num_rows')} rows")
            d = _data(await s.call_tool("get_query_history", {"fingerprint": fp or "x"}))
            check("get_query_history (empty log)", d.get("runs") == 0, f"{d.get('runs')} runs")
            res = await s.call_tool("workload_report", {})
            check("workload_report", not res.is_error, f"keys: {', '.join(sorted(_data(res))[:5])}")
            d = _data(await s.call_tool("registry_status", {}))
            check("registry_status", d.get("built") and not d.get("stale_tables"),
                  f"{len(d.get('tables', []))} tables, stale {d.get('stale_tables')}")
    return out


def main() -> int:
    from query_preflight.config import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.environ.get("GCP_PROJECT"))
    ap.add_argument("--dataset", default="cost_guard_thelook")
    args = ap.parse_args()
    if not args.project:
        raise SystemExit("needs --project or GCP_PROJECT")
    sys.path.insert(0, str(ROOT / "eval"))
    from code_version import code_version

    version = code_version()  # before the run: what is measured
    started = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        results = asyncio.run(smoke(args.project, args.dataset, Path(tmp)))
    from google.cloud import bigquery

    jobs = list(bigquery.Client(project=args.project).list_jobs(min_creation_time=started))
    results.append(("no BigQuery job ran (dry runs only)", not jobs, f"{len(jobs)} jobs"))
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<40} {detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    # Stored, so a quoted score has a file behind it (details are verdict reasons and
    # counts, no SQL text: the probe queries are the script's own).
    dest = ROOT / "eval" / "results" / f"mcp_smoke_bigquery_{started:%Y%m%d_%H%M%S}.json"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps({
        "ts": started.isoformat(), "code_version": version, "dataset": args.dataset,
        "passed": f"{len(results) - failed}/{len(results)}",
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]}, indent=2))
    print(f"results -> {dest.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
