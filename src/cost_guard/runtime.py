"""Runtime enforcement (F4.2): Guard -> watchdog execution -> query log.

The pre-execution Guard can't see everything (DuckDB estimates range filters
at a flat selectivity), so every query that runs gets a timeout: the learned
per-fingerprint threshold when one is enforced, else the workload class's
hard cap (customer.yaml max_runtime_sec). Only read-only queries reach
execution — the Guard blocks everything else — so killing one is safe.

DuckDB: connection.interrupt() from a timer thread (backend.execute).
BigQuery equivalent: QueryJobConfig(job_timeout_ms=...) plus
maximum_bytes_billed; the job is cancelled server-side at the deadline.

The query log stores literal-free fingerprints (the normalized query shape,
which stays in-customer) and numbers — never raw SQL or its literal values.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .backends.base import ExecutionResult
from .guard import GuardVerdict, decide
from .thresholds import fingerprint_query

DEFAULT_LOG = Path("runs/query_log.jsonl")


@dataclass
class LogRecord:
    ts: str
    fingerprint: str
    workload_class: str
    decision: str
    status: str  # "completed" | "killed" | "error" | "blocked"
    duration_sec: Optional[float]
    rows_estimate: Optional[int]
    timeout_sec: Optional[float]


def append_log(rec: LogRecord, path: str | Path = DEFAULT_LOG) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(asdict(rec)) + "\n")


def read_log(path: str | Path = DEFAULT_LOG) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


@dataclass
class RunOutcome:
    verdict: GuardVerdict
    execution: Optional[ExecutionResult]
    timeout_sec: Optional[float]
    fingerprint: str


def run_query(
    sql: str,
    backend,
    workload_class: str = "interactive",
    customer: Optional[dict] = None,
    updater=None,
    log_path: str | Path | None = DEFAULT_LOG,
    tracer=None,
) -> RunOutcome:
    """Guard the query, run it under the watchdog unless blocked, log it,
    and feed the duration to the threshold updater's shadow/probation."""
    from .config import caps_for, load_customer

    customer = customer if customer is not None else load_customer()
    fp = fingerprint_query(sql)
    verdict = decide(sql, workload_class, customer=customer, backend=backend, tracer=tracer)
    hard_cap = float(caps_for(customer, workload_class).get("max_runtime_sec") or 0) or None
    timeout = hard_cap
    if updater is not None and hard_cap is not None:
        timeout = updater.rules.threshold(fp, hard_cap)

    execution = None
    if verdict.decision != "block":
        execution = backend.execute(sql, timeout_sec=timeout)
        if updater is not None and execution.status in ("completed", "killed"):
            updater.observe(fp, execution.duration_sec)
    if log_path is not None:
        append_log(
            LogRecord(
                ts=datetime.now(timezone.utc).isoformat(),
                fingerprint=fp,
                workload_class=workload_class,
                decision=verdict.decision,
                status=execution.status if execution else "blocked",
                duration_sec=round(execution.duration_sec, 4) if execution else None,
                rows_estimate=verdict.estimated_rows,
                timeout_sec=timeout,
            ),
            log_path,
        )
    return RunOutcome(verdict, execution, timeout, fp)
