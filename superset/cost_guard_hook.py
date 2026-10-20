"""Cost Guard inside Apache Superset: every SQL Lab / chart query on the
BigQuery database is checked before Superset sends it (SQL_QUERY_MUTATOR).

A BLOCK stops the query: Superset shows Cost Guard's reason and fix hints as
the query's error. allow / warn / rewrite pass the query through unchanged,
the same as `cost_guard run` (a rewrite is offered, never swapped in).

Superset and Cost Guard pin different library versions (sqlglot among them),
so the check runs in Cost Guard's own venv as a subprocess: `cost_guard check`
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
log = logging.getLogger("cost_guard")


class CostGuardBlocked(Exception):
    """Raised for a blocked query; Superset shows the message as the error."""


def check(sql: str) -> dict:
    """Cost Guard's verdict for `sql` (the CLI's JSON)."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    out = subprocess.run([str(PYTHON), "-m", "cost_guard", "check", sql], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=TIMEOUT_SEC)
    start = out.stdout.find("{")
    if start < 0:
        raise RuntimeError((out.stderr.strip().splitlines() or ["no verdict"])[-1][:300])
    return json.loads(out.stdout[start:])


def block_message(v: dict) -> str:
    lines = [f"Cost Guard blocked this query: {v['reason']}"]
    # Causes and fixes only: Superset adds a LIMIT to SQL Lab queries, so the
    # "LIMIT doesn't reduce bytes" info hint is about Superset, not the user.
    hints = [h for h in v.get("fix_hints") or [] if h.get("kind") != "info"]
    lines += [f"- {h['text']}" for h in hints[:3]]
    return "\n".join(lines)


def mutator(sql: str, **kwargs) -> str:
    """SQL_QUERY_MUTATOR: check, stop a block, otherwise return `sql` unchanged."""
    database = kwargs.get("database")
    if database is not None and getattr(database, "backend", None) != "bigquery":
        return sql
    try:
        v = check(sql)
    except Exception as e:  # noqa: BLE001 — no verdict: not stopped, logged
        log.warning("Cost Guard check failed, query not stopped (%s)", type(e).__name__)
        return sql
    log.info("Cost Guard: %s", v["decision"])  # no reason: it can quote the query's literals
    if v["decision"] == "block":
        raise CostGuardBlocked(block_message(v))
    return sql
