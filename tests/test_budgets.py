"""Spend budgets per user, team and workload class (budgets.py, 2026-10-18)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from query_preflight import budgets
from query_preflight.backends.bigquery_backend import BigQueryWarehouse
from query_preflight.guard import decide
from query_preflight.runtime import run_query
from query_preflight.tracing import Tracer

TB = 1024**4
NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def _customer(**budget):
    return {"workload_classes": {
                "interactive": {"max_bytes_scanned": 200 * 1024**3, "max_runtime_sec": 60},
                "scheduled": {"max_bytes_scanned": TB, "max_runtime_sec": 600}},
            "cost_model": {"price_per_tb_usd": 6.25},
            "guard": {"allow_bypass": True},
            "budgets": {"enabled": True, "warn_at": 0.8, "user_usd": 10.0, "users": {},
                        "teams": {"analytics": {"usd": 15.0, "members": ["alice", "bob"]}},
                        "classes": {"scheduled": 100.0}, **budget}}


def _run(user, usd, ts=NOW - timedelta(days=1), cls="interactive", status="completed"):
    return {"ts": ts.isoformat(), "user": user, "workload_class": cls, "status": status,
            "bytes_billed": int(usd / 6.25 * TB), "decision": "allow"}


def test_spend_this_month_per_scope():
    log = [_run("alice", 4.0), _run("bob", 7.0), _run("carol", 9.0),
           _run("alice", 50.0, ts=datetime(2026, 9, 30, tzinfo=timezone.utc)),  # last month
           _run("alice", 3.0, status="blocked"),                                  # spent nothing
           _run("alice", 20.0, cls="scheduled")]                                  # its own budget
    st = budgets.budget_state(log, _customer(), "alice", "interactive", NOW)
    got = {(s.kind, s.name): (s.limit_usd, round(s.spent_usd, 4)) for s in st.scopes}
    assert got == {("user", "alice"): (10.0, 4.0), ("team", "analytics"): (15.0, 11.0)}
    st = budgets.budget_state(log, _customer(), "alice", "scheduled", NOW)
    assert [(s.kind, s.name, round(s.spent_usd, 4)) for s in st.scopes] == [("class", "scheduled", 20.0)]
    assert budgets.budget_state(log, _customer(enabled=False), "alice", "interactive", NOW) is None


def test_no_trusted_user_skips_user_and_team_budgets():
    st = budgets.budget_state([], _customer(), None, "interactive", NOW)
    assert st.scopes == [] and "no trusted user identity" in st.skipped


def test_the_stricter_scope_decides():
    st = budgets.BudgetState("2026-10-01", [budgets.Scope("user", "alice", 10.0, 4.0),
                                            budgets.Scope("team", "analytics", 15.0, 11.0)])
    level, scope, projected = budgets.judge(st, 1.0, 0.8)
    assert (level, scope.kind, projected) == ("warn", "team", 12.0)  # 80% of 15; user is at 50%
    level, scope, _ = budgets.judge(st, 4.5, 0.8)
    assert (level, scope.kind) == ("block", "team")
    assert budgets.judge(st, 0.0001, 0.8)[0] is None


class Client:
    """One 2 GiB table: a query costs 2 GiB x $6.25/TiB = ~$0.0122."""

    def __init__(self):
        self.ran = []

    def query(self, sql, job_config=None):
        if not job_config.dry_run:
            self.ran.append(job_config.maximum_bytes_billed)
        return SimpleNamespace(total_bytes_processed=2 * 1024**3, total_bytes_billed=2 * 1024**3,
                               result=lambda timeout=None, **kw: [], job_id="j", num_dml_affected_rows=None,
                               slot_millis=1, cache_hit=False)

    def get_table(self, ref):
        return SimpleNamespace(table_type="TABLE", num_rows=10, num_bytes=10, clustering_fields=None,
                               schema=[SimpleNamespace(name="a"), SimpleNamespace(name="created_at")],
                               time_partitioning=None, range_partitioning=None)


SQL = "SELECT a FROM t"


def _state(spent):
    return budgets.BudgetState("2026-10-01", [budgets.Scope("user", "alice", 0.05, spent)])


def test_guard_warns_then_blocks_on_budget(tmp_path):
    wh = BigQueryWarehouse("p", "ds", client=Client(), registry_path=str(tmp_path / "none"))
    c = _customer()
    v = decide(SQL, customer=c, backend=wh, tracer=Tracer(), budget=_state(0.0))
    assert v.decision == "allow" and v.budget["level"] is None
    v = decide(SQL, customer=c, backend=wh, tracer=Tracer(), budget=_state(0.03))
    assert v.decision == "warn" and "of the user budget 'alice'" in v.reason  # 0.0422 / 0.05 = 84%
    v = decide(SQL, customer=c, backend=wh, tracer=Tracer(), budget=_state(0.045))
    assert v.decision == "block" and v.reason.startswith("over the user budget 'alice'")
    assert v.budget_only_block and v.fix_hints[0]["kind"] == "cause"
    assert "resets on the 1st" in v.reason


def test_budget_approval_keeps_the_per_query_bytes_cap(tmp_path, monkeypatch):
    """A bypass of a budget-only block runs, but under the class's bytes ceiling."""
    client = Client()
    wh = BigQueryWarehouse("p", "ds", client=client, registry_path=str(tmp_path / "none"))
    log = tmp_path / "log.jsonl"
    monkeypatch.setattr(budgets, "budget_state", lambda *a, **k: _state(0.045))
    out = run_query(SQL, wh, customer=_customer(), log_path=log, user="alice",
                    bypass_reason="month-end close, approved by finance")
    assert out.verdict.bypassed and out.verdict.budget_only_block
    assert client.ran == [200 * 1024**3]  # the interactive cap, not lifted


def test_mcp_server_runs_as_the_deployment_user(synthetic_wh, tmp_path):
    from query_preflight.config import load_customer
    from query_preflight.mcp_server import QueryPreflightService

    svc = QueryPreflightService(synthetic_wh, load_customer(), log_path=tmp_path / "log.jsonl",
                           trace_path=None, investigations=False, user="alice")
    with pytest.raises(PermissionError, match="fixed user"):
        svc.run_query("SELECT 1", user="mallory")
    svc.run_query("SELECT 1")
    from query_preflight.runtime import read_log

    assert read_log(tmp_path / "log.jsonl")[-1]["user"] == "alice"
    rep = svc.workload_report()
    assert "budgets" not in rep and [s["name"] for s in rep["budget"]["scopes"]] == ["alice"]
    anon = QueryPreflightService(synthetic_wh, load_customer(), log_path=tmp_path / "log2.jsonl",
                            trace_path=None, investigations=False)
    out = anon.check_query("SELECT 1")
    assert any(s.startswith("budget: no trusted user identity") for s in out["skipped_rules"])
