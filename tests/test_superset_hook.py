"""The Superset hook (superset/query_preflight_hook.py): a block stops the query
with the reason and hints; anything else, or a failed check, passes the SQL
through unchanged. Superset itself is not needed (it runs in its own venv)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "superset"))
import query_preflight_hook as hook  # noqa: E402

BQ = SimpleNamespace(backend="bigquery")


def _verdict(decision, hints=()):
    return {"decision": decision, "reason": f"{decision} reason",
            "fix_hints": [{"kind": k, "text": h} for k, h in hints]}


def test_block_stops_the_query_with_reason_and_hints(monkeypatch):
    monkeypatch.setattr(hook, "check", lambda sql: _verdict(
        "block", [("info", "LIMIT doesn't reduce bytes"), ("cause", "aggregate first"), ("fix", "add a date")]))
    with pytest.raises(hook.QueryPreflightBlocked) as e:
        hook.mutator("SELECT 1", database=BQ)
    assert "Query Preflight blocked this query: block reason" in str(e.value)
    assert "- aggregate first\n- add a date" in str(e.value) and "LIMIT" not in str(e.value)


@pytest.mark.parametrize("decision", ["allow", "warn", "rewrite"])
def test_other_verdicts_pass_the_sql_unchanged(monkeypatch, decision):
    monkeypatch.setattr(hook, "check", lambda sql: _verdict(decision))
    assert hook.mutator("SELECT 1", database=BQ) == "SELECT 1"


def test_failed_check_does_not_stop_the_query(monkeypatch):
    def broken(sql):
        raise RuntimeError("no network")
    monkeypatch.setattr(hook, "check", broken)
    assert hook.mutator("SELECT 1", database=BQ) == "SELECT 1"


def test_other_databases_are_not_checked(monkeypatch):
    monkeypatch.setattr(hook, "check", lambda sql: pytest.fail("checked a non-BigQuery query"))
    assert hook.mutator("SELECT 1", database=SimpleNamespace(backend="postgresql")) == "SELECT 1"
