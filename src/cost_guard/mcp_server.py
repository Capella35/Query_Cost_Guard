"""MCP server: Cost Guard's tools for AI apps (DESIGN.md section 14, item 5).

    python -m cost_guard.mcp_server                      # DuckDB (customer.yaml)
    python -m cost_guard.mcp_server --backend bigquery   # GCP_PROJECT from .env

Five tools, over stdio:

  check_query        Guard verdict; nothing executes (BigQuery: a free dry run).
                     A block carries fix_hints: main cause first, then fixes.
  run_query          Guard, then execute under the watchdog — the ONLY path to
                     the warehouse this server offers. Blocked queries never run.
  get_table_info     size, partitioning, clustering of one table
  get_query_history  past runs of one query shape (fingerprint): numbers only
  workload_report    the query-log report (ledger.py) + Guard health + LLM usage

Boundaries, enforced here and not left to the calling model:
- the deployment sets the workload class (--workload): a caller may ask for an
  equal or stricter class, never a looser one — otherwise a blocked query
  could be retried as "scheduled" (10x the caps) and run;
- no bypass: a blocked query stays blocked. Bypassing a cap is a person's
  decision (CLI, with guard.allow_bypass), not something a model asks for;
- read-only SQL only (the Guard blocks anything else);
- no result rows ever leave the server: run_query returns status, duration,
  row count and bytes; history returns numbers, not users or query hashes;
- the server holds one warehouse backend and exposes no other route to it.

When a completed run breaks its shape's own history (anomaly.py), run_query
says so (`anomaly`) and the report-only investigator agent
(investigator_agent.py) starts in the background — the response does not
wait for it. Reports land in runs/investigations/; nothing is changed.

Every tool call is one trace: a root span mcp.<tool> with the Guard's spans
under it, appended to the trace file (runs/traces.jsonl) after the call, and
its trace_id returned in the response. With PHOENIX_COLLECTOR_ENDPOINT set
(e.g. http://localhost:6006 for a local `phoenix serve`) the same spans, and
the MCP SDK's own request spans, are exported to Phoenix (observability.py).
Spans carry a shape_id (hash of the fingerprint), not the fingerprint text.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import statistics
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .tracing import Tracer

DEFAULT_TRACES = Path("runs/traces.jsonl")
HISTORY_FIELDS = ("ts", "workload_class", "decision", "status", "duration_sec", "rows_estimate",
                  "bytes_estimate", "bytes_billed", "timeout_sec", "check_ms", "bypassed",
                  "anomaly_size", "anomaly")


def shape_id(fingerprint: str) -> str:
    """Short, stable id for a query shape — safe to put in exported spans."""
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:12]


class CostGuardService:
    """The tools as plain methods (testable without MCP). One instance per
    server, shared by concurrent calls: the backends are thread-safe and
    every call gets its own Tracer."""

    def __init__(self, backend, customer: dict, log_path: str | Path = "runs/query_log.jsonl",
                 trace_path: str | Path | None = DEFAULT_TRACES, llm=None,
                 workload_class: str = "interactive", investigations: bool = True,
                 investigation_dir: str | Path | None = None, agent_llm=None):
        from concurrent.futures import ThreadPoolExecutor

        from .config import caps_for

        caps_for(customer, workload_class)  # unknown class fails at startup, not per call
        self.agent = None
        self._agent_pool = None
        if investigations:
            from .investigator_agent import InvestigatorAgent

            # Reports sit next to the query log they investigate.
            self.agent = InvestigatorAgent(
                self, customer, investigation_dir or Path(log_path).parent / "investigations",
                llm=agent_llm)
            # One worker: investigations queue up instead of competing with queries.
            self._agent_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="investigator")
        self.pending: list = []  # futures of started investigations (tests wait on them)
        self.backend = backend
        self.customer = customer
        self.workload_class = workload_class
        self.log_path = Path(log_path)
        self.trace_path = Path(trace_path) if trace_path is not None else None
        self.llm = llm
        self.rewrite_wait_sec = (customer.get("guard") or {}).get("rewrite_wait_sec")

    def _workload(self, requested: Optional[str]) -> str:
        """The class a call runs under: the server's, or a stricter one asked for."""
        from .config import no_looser_than

        if requested is None or requested == self.workload_class:
            return self.workload_class
        if not no_looser_than(self.customer, requested, self.workload_class):
            raise PermissionError(
                f"workload_class {requested!r} has looser caps than this server's "
                f"{self.workload_class!r}; the deployment sets the class, a caller can only "
                "pick an equal or stricter one")
        return requested

    def _traced(self, tool: str, fn, **attrs) -> dict:
        tracer = Tracer()
        try:
            with tracer.trace(f"mcp.{tool}", **attrs) as tid:
                out = fn(tracer)
            out["trace_id"] = tid
            return out
        finally:
            if self.trace_path is not None:
                tracer.save(self.trace_path)

    def check_query(self, sql: str, workload_class: Optional[str] = None) -> dict:
        from .guard import decide
        from .thresholds import fingerprint_query

        workload_class = self._workload(workload_class)

        def go(tracer):
            fp = fingerprint_query(sql)
            v = decide(sql, workload_class, customer=self.customer, backend=self.backend,
                       tracer=tracer, llm=self.llm, rewrite_wait_sec=self.rewrite_wait_sec)
            _annotate_verdict(tracer, v, fp)
            return {**_verdict_fields(v), "fingerprint": fp, "rules_fired": _rules_fired(tracer)}

        return self._traced("check_query", go, workload_class=workload_class)

    def run_query(self, sql: str, workload_class: Optional[str] = None,
                  user: Optional[str] = None) -> dict:
        from .runtime import run_query

        workload_class = self._workload(workload_class)

        started: dict = {}

        def on_anomaly(event: dict) -> None:
            if self.agent is None:
                started["investigation"] = "off"
                return
            ok, why = self.agent.should_investigate(event["fingerprint"], event["ts"])
            started["investigation"] = "started" if ok else f"skipped: {why}"
            if ok:
                self.pending.append(self._agent_pool.submit(self.agent.investigate, event))

        def go(tracer):
            out = run_query(sql, self.backend, workload_class, customer=self.customer,
                            log_path=self.log_path, tracer=tracer, user=user, llm=self.llm,
                            rewrite_wait_sec=self.rewrite_wait_sec, on_anomaly=on_anomaly)
            _annotate_verdict(tracer, out.verdict, out.fingerprint)
            ex = out.execution
            from .tracing import annotate

            annotate(status=ex.status if ex else "blocked")
            return {
                **_verdict_fields(out.verdict),
                "fingerprint": out.fingerprint,
                "executed": ex is not None,
                "status": ex.status if ex else "blocked",
                "duration_sec": round(ex.duration_sec, 3) if ex else None,
                "row_count": ex.row_count if ex else None,  # a count — rows never leave the server
                "bytes_billed": ex.bytes_billed if ex else None,
                "detail": ex.detail if ex else None,
                "timeout_sec": out.timeout_sec,
                # The run broke its shape's own history (never blocks anything);
                # the report-only investigator runs in the background.
                "anomaly": out.anomaly.reason if out.anomaly else None,
                "investigation": started.get("investigation"),
            }

        return self._traced("run_query", go, workload_class=workload_class)

    def get_table_info(self, table: str) -> dict:
        from .tools.get_table_metadata import get_table_metadata

        return self._traced("get_table_info", lambda tracer: asdict(
            get_table_metadata(table, backend=self.backend, customer=self.customer)), table=table)

    def get_query_history(self, fingerprint: str, since: Optional[str] = None, limit: int = 100) -> dict:
        from .tools.get_query_history import get_query_history

        def go(tracer):
            rows = get_query_history(fingerprint, since=since, log_path=self.log_path)
            done = [r["duration_sec"] for r in rows
                    if r["status"] == "completed" and r.get("duration_sec") is not None]
            statuses: dict[str, int] = {}
            for r in rows:
                statuses[r["status"]] = statuses.get(r["status"], 0) + 1
            return {
                "fingerprint": fingerprint,
                "runs": len(rows),
                "users": len({r["user"] for r in rows if r.get("user")}),
                "statuses": statuses,
                "median_duration_sec": round(statistics.median(done), 4) if done else None,
                "max_duration_sec": round(max(done), 4) if done else None,
                "recent": [{k: r.get(k) for k in HISTORY_FIELDS} for r in rows[-limit:]],
            }

        return self._traced("get_query_history", go, shape_id=shape_id(fingerprint))

    def workload_report(self, top: int = 10) -> dict:
        from .ledger import build_report
        from .runtime import read_log
        from .tracing import read_traces

        def go(tracer):
            traces = read_traces(self.trace_path) if self.trace_path is not None else None
            rep = build_report(read_log(self.log_path), self.customer, top, traces=traces)
            for row in rep.get("workload_by_shape", []):  # shape text stays local; ids travel
                if row.get("fingerprint"):
                    row["shape_id"] = shape_id(row["fingerprint"])
            return rep

        return self._traced("workload_report", go, top=top)


def _rules_fired(tracer) -> list[str]:
    """Names of the rules that produced findings (from the call's rule spans)."""
    return sorted({s["name"][len("rule."):] for s in tracer.spans
                   if s["name"].startswith("rule.") and s["attrs"].get("findings")})


def _verdict_fields(v) -> dict:
    return {
        "decision": v.decision,
        "reason": v.reason,
        "findings": v.findings,
        "skipped_rules": v.skipped_rules,
        "estimated_bytes": v.estimated_bytes,
        "estimated_rows": v.estimated_rows,
        "estimated_runtime_sec": v.estimated_runtime_sec,
        "estimate_source": v.estimate_source,
        "rewritten_sql": v.rewritten_sql,  # the caller's own query, rewritten — returned, not traced
        "rewrite_source": v.rewrite_source,
        "verification": v.verification,
        "explanation": v.explanation,
        "fix_hints": v.fix_hints,  # blocks: main cause first, then fixes
        "caps": v.caps,
        "workload_class": v.workload_class,
    }


def _annotate_verdict(tracer, v, fp: str) -> None:
    from .tracing import annotate

    annotate(shape_id=shape_id(fp), decision=v.decision, findings=len(v.findings),
             estimated_bytes=v.estimated_bytes, estimated_rows=v.estimated_rows,
             rewrite_offered=v.rewritten_sql is not None, bypassed=v.bypassed is not None)


def build_server(service: CostGuardService):
    """The MCP server: one tool per service method, with tool annotations."""
    from mcp.server import MCPServer
    from mcp_types import ToolAnnotations

    server = MCPServer(
        "cost-guard",
        instructions=(
            "Cost Guard checks warehouse SQL for cost before it runs. Call check_query "
            "before run_query when unsure; run_query is the only way to execute SQL here, "
            "and blocked queries never run. Results are summaries (status, row count, "
            "bytes), never rows. Read-only SELECT/WITH queries only."),
    )
    from mcp.server.mcpserver.exceptions import ToolError

    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    def call(fn, *args):
        # A refusal the caller should read and act on (a looser workload class, an
        # unknown one) reaches the model with its message; the SDK withholds the
        # text of any other exception (a crash) from the client.
        try:
            return fn(*args)
        except (PermissionError, KeyError, ValueError) as e:
            raise ToolError(str(e).strip("'\"")) from e

    @server.tool(annotations=read_only)
    def check_query(sql: str, workload_class: Optional[str] = None) -> dict[str, Any]:
        """Check a read-only SQL query's cost WITHOUT running it. Returns the Guard's
        decision (allow | warn | rewrite | block), the reason and findings, estimated
        bytes/rows, a verified cheaper rewrite when one exists, and the query's
        fingerprint (for get_query_history). A block carries fix_hints: the main cause
        first, then fixes, each marked whether it changes the result — revise the query
        and check again. workload_class: omit to use this server's class; only an equal
        or stricter class is accepted."""
        return call(service.check_query, sql, workload_class)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=False, open_world_hint=False))
    def run_query(sql: str, workload_class: Optional[str] = None,
                  user: Optional[str] = None) -> dict[str, Any]:
        """Run a read-only SQL query through the Guard: blocked queries never run (there
        is no bypass here; see fix_hints to revise them); others run under a runtime
        limit and are logged. Returns the decision, run status, duration, row COUNT and
        billed bytes — never result rows. Costs warehouse money when it runs.
        user: opaque caller id. workload_class: as in check_query."""
        return call(service.run_query, sql, workload_class, user)

    @server.tool(annotations=read_only)
    def get_table_info(table: str) -> dict[str, Any]:
        """Size (rows, bytes), partition column, clustering and column count of a table."""
        return call(service.get_table_info, table)

    @server.tool(annotations=read_only)
    def get_query_history(fingerprint: str, since: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
        """Past runs of one query shape (the fingerprint from check_query/run_query):
        counts, statuses, median/max duration, and the most recent runs' numbers.
        since: ISO-8601 timestamp."""
        return call(service.get_query_history, fingerprint, since, limit)

    @server.tool(annotations=read_only)
    def workload_report(top: int = 10) -> dict[str, Any]:
        """Workload report from the query log: most expensive query shapes, warn
        follow-through, result-cache opportunity, spend ledger (MODELED dollars), Guard
        health (decision mix, block/bypass rates, check latency, estimate accuracy) and
        LLM usage."""
        return call(service.workload_report, top)

    return server


def _backend(args, customer):
    if args.backend == "bigquery":
        from .backends.bigquery_backend import BigQueryWarehouse

        project = args.project or os.environ.get("GCP_PROJECT")
        if not project:
            raise SystemExit("--backend bigquery needs --project or GCP_PROJECT")
        return BigQueryWarehouse(project, args.dataset)
    from .backends.duckdb_backend import DuckDBWarehouse

    return DuckDBWarehouse(args.db or customer["warehouse"]["duckdb"]["db_path"])


def main(argv: list[str] | None = None) -> int:
    from . import observability
    from .config import load_customer, load_dotenv

    load_dotenv()
    p = argparse.ArgumentParser(prog="cost_guard.mcp_server", description=__doc__.split("\n")[0])
    p.add_argument("--backend", choices=["duckdb", "bigquery"],
                   default=os.environ.get("COST_GUARD_BACKEND", "duckdb"))
    p.add_argument("--db", help="DuckDB file (default: from customer.yaml)")
    p.add_argument("--project", help="BigQuery project (default: GCP_PROJECT)")
    p.add_argument("--dataset", default="thelook_ecommerce")
    p.add_argument("--workload", default=os.environ.get("COST_GUARD_WORKLOAD", "interactive"),
                   help="workload class for every call (callers may only pick a stricter one)")
    p.add_argument("--customer", help="path to customer.yaml (default: repo root)")
    p.add_argument("--llm", choices=["none", "claude"], default="none")
    p.add_argument("--log", default="runs/query_log.jsonl")
    p.add_argument("--traces", default=str(DEFAULT_TRACES))
    p.add_argument("--no-investigations", action="store_true",
                   help="do not start the investigator agent on anomalies")
    args = p.parse_args(argv)

    customer = load_customer(args.customer)
    backend = _backend(args, customer)
    llm = None
    if args.llm == "claude":
        from .llm import ClaudeLLM

        llm = ClaudeLLM(backend=backend)
    if os.environ.get("PHOENIX_COLLECTOR_ENDPOINT"):
        observability.setup()
    agent_llm = None
    from .investigator_agent import settings as agent_settings

    if agent_settings(customer)["llm_max_per_day"] > 0:
        from .llm import ClaudeLLM

        agent_llm = llm or ClaudeLLM(backend=backend)
    service = CostGuardService(backend, customer, args.log, args.traces, llm, args.workload,
                               investigations=not args.no_investigations, agent_llm=agent_llm)
    try:
        build_server(service).run("stdio")
    finally:
        observability.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
