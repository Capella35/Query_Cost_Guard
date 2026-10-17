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
Two fields exist for the workload report (ledger.py):
- sql_hash: a KEYED hash (HMAC-SHA256) of the exact query, literals
  included. It answers "was this identical query run before?" (result-cache
  opportunity) without storing the literals; the key stays with the
  deployment (COST_GUARD_LOG_KEY, else a generated key file next to the log),
  so a guessable literal like user_id = 5 can't be confirmed by hashing
  candidates without it.
- user: an opaque caller id, as passed by the caller (pseudonymize upstream).
  Needed to tell whether a warned analyst changed their next query.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
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
    # Added for the workload report (ledger.py); absent in older records.
    user: Optional[str] = None
    sql_hash: Optional[str] = None
    rewrite_fingerprint: Optional[str] = None  # shape of the offered rewrite
    bytes_estimate: Optional[int] = None
    bypassed: bool = False
    check_ms: Optional[float] = None  # time the Guard took to decide
    bytes_billed: Optional[int] = None  # actual billed bytes (BigQuery), vs bytes_estimate


def _log_key(log_path: Path) -> bytes:
    env = os.environ.get("COST_GUARD_LOG_KEY")
    if env:
        return env.encode()
    key_file = log_path.parent / ".log_key"
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(secrets.token_hex(32))
        key_file.chmod(0o600)
    return key_file.read_text().strip().encode()


def query_hash(sql: str, log_path: str | Path = DEFAULT_LOG) -> str:
    """Keyed hash of the exact query (whitespace-normalized, literals kept)."""
    normalized = " ".join(sql.split())
    return hmac.new(_log_key(Path(log_path)), normalized.encode(), hashlib.sha256).hexdigest()[:32]


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
    user: Optional[str] = None,
    bypass_reason: Optional[str] = None,
    llm=None,
    rewrite_wait_sec: Optional[float] = None,
) -> RunOutcome:
    """Guard the query, run it under the watchdog unless blocked, log it,
    and feed the duration to the threshold updater's shadow/probation.

    Rewrites already offered (rewrite_fingerprint in the log) are passed to
    the Guard so a run rewrite is not rewritten again. The log is read whole
    each call — fine for a POC log, an index for a real one.
    rewrite_wait_sec: see guard.decide (long-running services pass
    customer.yaml guard.rewrite_wait_sec)."""
    import time

    from .config import caps_for, load_customer

    customer = customer if customer is not None else load_customer()
    fp = fingerprint_query(sql)
    offered = ({r["rewrite_fingerprint"] for r in read_log(log_path) if r.get("rewrite_fingerprint")}
               if log_path is not None else None)
    t0 = time.perf_counter()
    verdict = decide(sql, workload_class, customer=customer, backend=backend, tracer=tracer,
                     llm=llm, bypass_reason=bypass_reason, offered_rewrites=offered,
                     rewrite_wait_sec=rewrite_wait_sec)
    check_ms = round((time.perf_counter() - t0) * 1000, 1)
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
                user=user,
                sql_hash=query_hash(sql, log_path),
                rewrite_fingerprint=(fingerprint_query(verdict.rewritten_sql)
                                     if verdict.rewritten_sql else None),
                bytes_estimate=verdict.estimated_bytes,
                bypassed=verdict.bypassed is not None,
                check_ms=check_ms,
                bytes_billed=execution.bytes_billed if execution else None,
            ),
            log_path,
        )
    return RunOutcome(verdict, execution, timeout, fp)
