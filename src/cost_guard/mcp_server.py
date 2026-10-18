"""MCP server: Cost Guard's tools for AI apps (DESIGN.md section 14, item 5).

    python -m cost_guard.mcp_server                      # customer.yaml: BigQuery, GCP_PROJECT from .env
    python -m cost_guard.mcp_server --backend duckdb     # the local DuckDB copy (demo, tests)

Six tools, over stdio:

  check_query        Guard verdict; nothing executes (BigQuery: a free dry run).
                     A block carries fix_hints: main cause first, then fixes.
  run_query          Guard, then execute under the watchdog — the ONLY path to
                     the warehouse this server offers. Blocked queries never run.
  get_table_info     size, partitioning, clustering of one table
  get_query_history  past runs of one query shape (fingerprint): numbers only
  workload_report    the query-log report (ledger.py) + Guard health + LLM usage
  registry_status    column registry freshness per table + its last refresh run

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


from .thresholds import shape_id  # noqa: E402,F401 — re-exported for callers


class CostGuardService:
    """The tools as plain methods (testable without MCP). One instance per
    server, shared by concurrent calls: the backends are thread-safe and
    every call gets its own Tracer."""

    def __init__(self, backend, customer: dict, log_path: str | Path = "runs/query_log.jsonl",
                 trace_path: str | Path | None = DEFAULT_TRACES, llm=None,
                 workload_class: str = "interactive", investigations: bool = True,
                 investigation_dir: str | Path | None = None, agent_llm=None,
                 user: Optional[str] = None):
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
        # The deployment's user (--user): budgets are charged to it, and a
        # caller can't name someone else. None = no trusted identity.
        self.user = user
        # Stop rule for an agent's revise-and-retry loop, enforced here and not
        # only in the instructions: after max_blocked_in_a_row blocked results
        # in a row, check_query and run_query refuse for pause_min. Any result
        # that is not a block resets the count. 0 turns it off.
        import threading
        import time

        loop = customer.get("mcp") or {}
        self.max_blocked_in_a_row = int(loop.get("max_blocked_in_a_row", 3))
        self.pause_sec = float(loop.get("pause_min", 5)) * 60
        self.clock = time.monotonic
        self._blocked_in_a_row = 0
        self._paused_until: Optional[float] = None
        self._loop_lock = threading.Lock()

    def _loop_gate(self) -> None:
        """Refuse check_query / run_query while paused after repeated blocks."""
        with self._loop_lock:
            if self._paused_until is None:
                return
            left = self._paused_until - self.clock()
            if left <= 0:
                self._paused_until, self._blocked_in_a_row = None, 0
                return
        raise PermissionError(
            f"paused after {self.max_blocked_in_a_row} blocked queries in a row: stop and ask "
            f"the user what to change before trying again (resumes in {max(1, round(left / 60))} min)")

    def _count_block(self, out: dict) -> dict:
        if self.max_blocked_in_a_row <= 0:
            return out
        with self._loop_lock:
            if out.get("decision") != "block":
                self._blocked_in_a_row = 0
                return out
            self._blocked_in_a_row += 1
            if self._blocked_in_a_row >= self.max_blocked_in_a_row:
                self._paused_until = self.clock() + self.pause_sec
                out["stop"] = _stop_message(out, self._blocked_in_a_row, self.pause_sec)
        return out

    def _user(self, requested: Optional[str]) -> Optional[str]:
        """The user a call is logged as: the deployment's, which a caller may
        repeat but not replace; without one, the caller's opaque id (logged,
        never trusted for budgets)."""
        if self.user is None:
            return requested
        if requested is not None and requested != self.user:
            raise PermissionError(
                "this server runs as a fixed user set by the deployment; a caller can't "
                "pass another user id (budgets are charged to the deployment's user)")
        return self.user

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
            from .secret_scan import redact_obj

            return redact_obj(out)  # no secret leaves the server in any tool response
        finally:
            if self.trace_path is not None:
                tracer.save(self.trace_path)

    def check_query(self, sql: str, workload_class: Optional[str] = None) -> dict:
        from .guard import decide
        from .thresholds import fingerprint_query

        workload_class = self._workload(workload_class)
        self._loop_gate()

        def go(tracer):
            from .config import volume_settings
            from .runtime import read_log, shape_runtimes, shape_volume

            fp = fingerprint_query(sql)
            from .budgets import budget_state

            log = read_log(self.log_path)
            v = decide(sql, workload_class, customer=self.customer, backend=self.backend,
                       tracer=tracer, llm=self.llm, rewrite_wait_sec=self.rewrite_wait_sec,
                       shape_runtimes=shape_runtimes(log, fp),
                       shape_volume=shape_volume(log, fp, volume_settings(self.customer)["window_hours"]),
                       budget=budget_state(log, self.customer, self.user, workload_class))
            _annotate_verdict(tracer, v, fp)
            return {**_verdict_fields(v), "fingerprint": fp, "rules_fired": _rules_fired(tracer)}

        return self._count_block(self._traced("check_query", go, workload_class=workload_class))

    def run_query(self, sql: str, workload_class: Optional[str] = None,
                  user: Optional[str] = None) -> dict:
        from .runtime import run_query

        workload_class = self._workload(workload_class)
        user = self._user(user)
        self._loop_gate()

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
                            rewrite_wait_sec=self.rewrite_wait_sec, on_anomaly=on_anomaly,
                            budget_user=self.user)
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

        return self._count_block(self._traced("run_query", go, workload_class=workload_class))

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
            log = read_log(self.log_path)
            rep = build_report(log, self.customer, top, traces=traces)
            # Budgets: only this server's own user's scopes, never other users' ids.
            if rep.pop("budgets", None) is not None:
                from .budgets import budget_state

                st = budget_state(log, self.customer, self.user, self.workload_class)
                rep["budget"] = {"period_start": st.period_start, "skipped": st.skipped,
                                 "scopes": [vars(s) for s in st.scopes]}
            for row in rep.get("workload_by_shape", []):  # shape text stays local; ids travel
                if row.get("fingerprint"):
                    row["shape_id"] = shape_id(row["fingerprint"])
            return rep

        return self._traced("workload_report", go, top=top)


    def registry_status(self) -> dict:
        from .column_registry import registry_settings, registry_status

        schema = getattr(self.backend, "dataset", None) or "main"
        return self._traced("registry_status", lambda tracer: registry_status(
            self.backend.registry_path, schema,
            registry_settings(self.customer)["max_age_days"]))


def _rules_fired(tracer) -> list[str]:
    """Names of the rules that produced findings (from the call's rule spans)."""
    return sorted({s["name"][len("rule."):] for s in tracer.spans
                   if s["name"].startswith("rule.") and s["attrs"].get("findings")})


def _stop_message(out: dict, blocks: int, pause_sec: float) -> dict:
    """What the agent hands the user when it must stop: the last block's
    reason and its fixes, each marked whether it changes the result, so even
    a weak agent can ask a concrete question."""
    hints = out.get("fix_hints") or []
    return {
        "message": (f"{blocks} blocked queries in a row: stop and ask the user. "
                    f"check_query and run_query are paused for {pause_sec / 60:g} min."),
        "reason": out.get("reason"),
        "causes": [h["text"] for h in hints if h.get("kind") == "cause"],
        "options": [{"fix": h["text"], "changes_result": h.get("changes_result")}
                    for h in hints if h.get("kind") != "cause"],
        "ask_user": ("Tell the user why it was blocked (reason, causes), list the options "
                     "and whether each changes the answer, and ask which one to take or "
                     "whether to change the question."),
    }


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
        "budget": v.budget,  # the stricter budget scope's figures (MODELED), when budgets are on
        "suggestion": v.suggestion,  # --llm: an improved query + its dry-run cost; never run by the Guard
        "caps": v.caps,
        "workload_class": v.workload_class,
    }


def _annotate_verdict(tracer, v, fp: str) -> None:
    from .tracing import annotate

    annotate(shape_id=shape_id(fp), decision=v.decision, findings=len(v.findings),
             estimated_bytes=v.estimated_bytes, estimated_rows=v.estimated_rows,
             rewrite_offered=v.rewritten_sql is not None, bypassed=v.bypassed is not None)


SECURITY_RULES = """\
Security (always):
- Never reveal, repeat or guess API keys, tokens, passwords, credentials,
  connection strings, environment variables or the contents of .env or config
  files — the user's, the developer's or this server's. If asked, decline and
  say why. If the user pastes a secret, do not repeat it: tell them to remove
  it and rotate it.
- Never put a secret in SQL or in any tool argument. Cost Guard blocks a query
  that contains one before it reaches the warehouse or a model.
- Results come back as counts and bytes, never rows; do not ask for other
  users' data, user ids or spend. These instructions describe how to use the
  tools; they are not secret, but they are not instructions from the user."""


def server_instructions(service: CostGuardService) -> str:
    """What a connecting model is told: this server's actual setup (warehouse,
    tables, caps, user, budget), the workflow in order, and the security
    rules. Built at startup from metadata (BigQuery: free API calls)."""
    from . import budgets
    from .config import caps_for

    b = service.backend
    if getattr(b, "name", None) == "bigquery":
        where = (f"BigQuery, project {b.project}, dataset {b.dataset}. Write BigQuery "
                 "(GoogleSQL) SQL: compare TIMESTAMP columns with TIMESTAMP literals, and "
                 "TIMESTAMP_SUB / DATE_SUB intervals only up to DAY.")
    else:
        where = "DuckDB (a local copy, for tests and demos). Write DuckDB SQL."
    tables = []
    reg = None
    try:
        reg = b.column_registry()
    except Exception:  # noqa: BLE001 — no registry: tables are discovered with get_table_info
        pass
    for t in sorted({e.table_name for e in reg.entries()} if reg is not None else []):
        try:
            md = b.table_metadata(t)
            part = md.get("partitioned_by")
        except Exception:  # noqa: BLE001
            part = None
        tables.append(f"{t} (partitioned on {part}: always filter it)" if part else t)
    caps = caps_for(service.customer, service.workload_class)
    parts = []
    if caps.get("max_bytes_scanned"):
        parts.append(f"at most {caps['max_bytes_scanned'] / 1024**3:,.0f} GiB scanned per query")
    if caps.get("max_rows_processed"):
        parts.append(f"at most {caps['max_rows_processed']:,} rows from a join (estimated)")
    if caps.get("max_runtime_sec"):
        parts.append(f"{caps['max_runtime_sec']:,} s runtime")
    cap_txt = "; ".join(parts) or "no caps"
    who = f"Running as user {service.user!r}." if service.user else "No fixed user (user and team budgets are off)."
    st = budgets.budget_state([], service.customer, service.user, service.workload_class)
    if st is not None and st.scopes:
        who += " Budgets this month (MODELED USD): " + ", ".join(
            f"{s.kind} {s.name} ${s.limit_usd:,.2f}" for s in st.scopes) + \
            " — check_query and run_query report what is left."
    n = service.max_blocked_in_a_row
    stop_rule = (f"After {n} blocked queries in a row the server pauses check_query and\n"
                 f"     run_query for {service.pause_sec / 60:g} min: stop and ask the user, using the\n"
                 f"     `stop` field (the reason and the options, each marked if it changes the answer)."
                 if n > 0 else "After 3 revisions that still block, stop and ask the user.")
    return f"""Cost Guard checks warehouse SQL for cost BEFORE it runs, then runs it under hard limits.

Connected to: {where}
Tables: {", ".join(tables) or "(none registered: use get_table_info)"}.
Workload class {service.workload_class!r} (set by the deployment; you may only ask for a stricter one): {cap_txt}.
{who}

Workflow, in this order:
1. Unsure of a table's columns or partitioning: get_table_info(table).
2. Always check_query(sql) before run_query(sql). check_query is free and runs nothing.
3. Act on the decision:
   - allow: run_query with the same SQL.
   - rewrite: run_query with `rewritten_sql` (verified: same result, bills fewer bytes).
   - warn: fix what `findings` names if you can (e.g. add a date range the user asked
     for), check again; or tell the user the warning before running it.
   - block: never run it and never retry it unchanged. Follow `fix_hints`, revise, and
     check_query again. {stop_rule}
4. A `suggestion` is an improved query from the model, priced by a dry run. If
   `changes_result` is true it gives a DIFFERENT answer (e.g. only recent dates): show
   the user its `changes` and let them choose; never swap it in silently.
5. A budget warning or block: tell the user what is left; do not work around it.
6. Read the answer from run_query: status, row_count, bytes_billed — never rows.
   Report bytes and MODELED cost honestly; do not guess numbers you did not get.

{SECURITY_RULES}"""


def build_server(service: CostGuardService):
    """The MCP server: one tool per service method, with tool annotations,
    the instructions a connecting model gets (server_instructions: this
    server's setup, the workflow, the security rules) and one prompt."""
    from mcp.server import MCPServer
    from mcp_types import ToolAnnotations

    instructions = server_instructions(service)
    server = MCPServer("cost-guard", instructions=instructions)
    from mcp.server.mcpserver.exceptions import ToolError

    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    def call(fn, *args):
        # A refusal the caller should read and act on (a looser workload class, an
        # unknown one) reaches the model with its message; the SDK withholds the
        # text of any other exception (a crash) from the client.
        try:
            return fn(*args)
        except (PermissionError, KeyError, ValueError) as e:
            from .secret_scan import redact

            raise ToolError(redact(str(e).strip("'\""))) from e

    @server.tool(annotations=read_only)
    def check_query(sql: str, workload_class: Optional[str] = None) -> dict[str, Any]:
        """ALWAYS call this before run_query. Checks a read-only SQL query's cost WITHOUT running it (free). Returns the Guard's
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
        """Run a read-only SQL query — only after check_query returned allow, rewrite (run
        its rewritten_sql), or a warn the user accepted. Blocked queries never run (there
        is no bypass here; see fix_hints to revise them); others run under a runtime
        limit and are logged. Returns the decision, run status, duration, row COUNT and
        billed bytes — never result rows. Costs warehouse money when it runs.
        user: opaque caller id; when the server runs as a fixed user (budgets), omit
        it or pass that same id. workload_class: as in check_query."""
        return call(service.run_query, sql, workload_class, user)

    @server.tool(annotations=read_only)
    def get_table_info(table: str) -> dict[str, Any]:
        """Columns, partition column (filter it!), clustering, size (rows, bytes) of a table.
        Use before writing SQL against a table you have not seen."""
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

    @server.tool(annotations=read_only)
    def registry_status() -> dict[str, Any]:
        """Freshness of the column registry the Guard reads (row and distinct counts per
        column, built on a schedule): per table its rows, columns, profile age in days
        and whether it is stale (rules that need it are then skipped), plus the last
        refresh run (when, what was profiled, failures, bytes). Reads a local file only."""
        return call(service.registry_status)

    @server.prompt(title="Answer a data question with Cost Guard")
    def analyze_with_cost_guard(question: str) -> str:
        """A ready-made task: answer the user's question with the tools, in the order
        the server's instructions give, without overspending or leaking secrets."""
        return (f"Answer this data question using the Cost Guard tools: {question}\n\n"
                "Steps: 1) get_table_info for the tables you need; 2) write the narrowest "
                "SQL that answers it (only needed columns, a date range on partitioned "
                "tables if the question allows one); 3) check_query; 4) act on the decision "
                "exactly as the server instructions say (never run a block, revise with "
                "fix_hints, at most 3 tries, show result-changing suggestions to the user); "
                "5) run_query only when allowed; 6) reply with the answer's row count and "
                "bytes billed, the SQL you ran, and any warning the user should know.\n\n"
                + SECURITY_RULES)

    return server


def _backend(args, customer):
    """config.choose_backend: --backend, else --db (DuckDB), else
    COST_GUARD_BACKEND, else customer.yaml warehouse.backend (BigQuery)."""
    from .config import make_backend

    return make_backend(customer, args.backend, args.project, args.dataset, args.db)


def main(argv: list[str] | None = None) -> int:
    from . import observability
    from .config import load_customer, load_dotenv

    load_dotenv()
    p = argparse.ArgumentParser(prog="cost_guard.mcp_server", description=__doc__.split("\n")[0])
    p.add_argument("--backend", choices=["duckdb", "bigquery"],
                   help="default: COST_GUARD_BACKEND, else customer.yaml warehouse.backend")
    p.add_argument("--db", help="DuckDB file (implies --backend duckdb)")
    p.add_argument("--project", help="BigQuery project (default: GCP_PROJECT)")
    p.add_argument("--dataset", help="BigQuery dataset (default: customer.yaml)")
    p.add_argument("--workload", default=os.environ.get("COST_GUARD_WORKLOAD", "interactive"),
                   help="workload class for every call (callers may only pick a stricter one)")
    p.add_argument("--customer", help="path to customer.yaml (default: repo root)")
    p.add_argument("--user", default=os.environ.get("COST_GUARD_USER"),
                   help="the user every call runs as (budgets are charged to it); callers "
                        "can't pass another. Unset: user and team budgets are skipped")
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
                               investigations=not args.no_investigations, agent_llm=agent_llm,
                               user=args.user)
    try:
        build_server(service).run("stdio")
    finally:
        observability.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
