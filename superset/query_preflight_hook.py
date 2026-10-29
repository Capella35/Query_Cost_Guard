"""Query Preflight inside Apache Superset: every SQL Lab / chart query on the
BigQuery database is checked before Superset sends it (SQL_QUERY_MUTATOR).

A BLOCK stops the query: Superset shows Query Preflight's reason and fix hints as
the query's error. A filter value that matches a stored value only when case
is ignored (`status = 'cancelled'`, stored 'Cancelled') also stops the query,
with "did you mean" and the corrected query: as written it returns no rows for
that value, and a silent empty chart reads as "there are none" (owner decision
2026-10-29). Nothing is swapped in. allow / warn / other rewrites pass the
query through unchanged, the same as `query_preflight run`.

Superset and Query Preflight pin different library versions (sqlglot among them),
so the check runs in Query Preflight's own venv as a subprocess: `query_preflight check`
(a free dry run; nothing executes). If the check itself fails (no network,
a timeout), the query is not stopped: the Guard's job is the verdict, and
BigQuery rejects invalid SQL on its own.
"""
import json
import logging
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "bin" / "python"
TIMEOUT_SEC = 60
log = logging.getLogger("query_preflight")


class QueryPreflightBlocked(Exception):
    """Raised for a blocked query; Superset shows the message as the error."""


class QueryPreflightValueCase(QueryPreflightBlocked):
    """Raised for a filter value that matches a stored value only in another case."""


def check(sql: str) -> dict:
    """Query Preflight's verdict for `sql` (the CLI's JSON)."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    out = subprocess.run([str(PYTHON), "-m", "query_preflight", "check", sql], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=TIMEOUT_SEC)
    start = out.stdout.find("{")
    if start < 0:
        raise RuntimeError((out.stderr.strip().splitlines() or ["no verdict"])[-1][:300])
    return json.loads(out.stdout[start:])


def block_message(v: dict) -> str:
    lines = [f"Query Preflight blocked this query: {v['reason']}"]
    # Causes and fixes only: Superset adds a LIMIT to SQL Lab queries, so the
    # "LIMIT doesn't reduce bytes" info hint is about Superset, not the user.
    hints = [h for h in v.get("fix_hints") or [] if h.get("kind") != "info"]
    lines += [f"- {h['text']}" for h in hints[:3]]
    return "\n".join(lines)


def value_case_message(v: dict) -> str:
    lines = ["Query Preflight stopped this query: a filter value matches no stored value "
             "except in another case, so it returns no rows for that value. Did you mean:"]
    lines += [f"- {n}" for n in v["value_case_fixes"]]
    lines += ["", "Corrected query (check it, then run it):", v["rewritten_sql"]]
    return "\n".join(lines)


def mutator(sql: str, **kwargs) -> str:
    """SQL_QUERY_MUTATOR: check, stop a block or a value-case miss, otherwise
    return `sql` unchanged."""
    database = kwargs.get("database")
    if database is not None and getattr(database, "backend", None) != "bigquery":
        return sql
    try:
        v = check(sql)
    except Exception as e:  # noqa: BLE001 — no verdict: not stopped, logged
        log.warning("Query Preflight check failed, query not stopped (%s)", type(e).__name__)
        return sql
    log.info("Query Preflight: %s", v["decision"])  # no reason: it can quote the query's literals
    if v["decision"] == "block":
        raise QueryPreflightBlocked(block_message(v))
    if v.get("value_case_fixes") and v.get("rewritten_sql"):
        raise QueryPreflightValueCase(value_case_message(v))
    return sql
