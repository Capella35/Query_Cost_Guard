"""MCP server: tool outputs, boundaries (no rows, run is the only path, read-only),
one trace per call, and the optional OpenTelemetry/Phoenix export."""

import asyncio
import json

import pytest

from cost_guard.config import load_customer
from cost_guard.mcp_server import CostGuardService, shape_id
from cost_guard.runtime import read_log
from cost_guard.tracing import read_traces

SMALL = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
HUGE = "SELECT * FROM users, orders WHERE users.email = 'alice@example.com'"


@pytest.fixture
def service(synthetic_wh, tmp_path):
    return CostGuardService(synthetic_wh, load_customer(), log_path=tmp_path / "log.jsonl",
                            trace_path=tmp_path / "traces.jsonl")


def test_check_query_runs_nothing_and_returns_a_trace(service):
    out = service.check_query(SMALL)
    assert out["decision"] in ("allow", "warn") and out["fingerprint"]
    assert read_log(service.log_path) == []  # a check never executes or logs a run
    spans = read_traces(service.trace_path)
    root = next(s for s in spans if s["name"] == "mcp.check_query")
    decide = next(s for s in spans if s["name"] == "guard.decide")
    assert {s["trace_id"] for s in spans} == {out["trace_id"]}
    assert root["parent_id"] is None and decide["parent_id"] == root["span_id"]
    assert root["attrs"]["shape_id"] == shape_id(out["fingerprint"])
    assert root["attrs"]["decision"] == out["decision"]


def test_run_query_returns_counts_never_rows(service):
    out = service.run_query(SMALL, user="u1")
    assert out["executed"] and out["status"] == "completed" and out["row_count"] > 0
    assert not any(k in out for k in ("rows", "result", "data"))
    assert read_log(service.log_path)[0]["check_ms"] > 0


def test_blocked_and_non_read_only_queries_never_run(service):
    assert service.run_query(HUGE)["executed"] is False
    out = service.run_query("DELETE FROM orders")
    assert out["decision"] == "block" and out["executed"] is False
    assert [r["status"] for r in read_log(service.log_path)] == ["blocked", "blocked"]
    # Literals and SQL text stay out of the trace file.
    text = service.trace_path.read_text().lower()
    assert "alice" not in text and "users.email" not in text and "delete from" not in text


def test_history_is_numbers_only(service):
    fp = service.run_query(SMALL, user="u1")["fingerprint"]
    service.run_query(SMALL, user="u2")
    h = service.get_query_history(fp)
    assert h["runs"] == 2 and h["users"] == 2 and h["statuses"] == {"completed": 2}
    assert h["median_duration_sec"] is not None
    assert all("user" not in r and "sql_hash" not in r for r in h["recent"])


def test_table_info(service):
    md = service.get_table_info("orders")
    assert md["num_rows"] == 1_000_000 and md["partitioned_by"] == "created_at"


def test_workload_report_has_health_and_llm_usage(service):
    service.run_query(SMALL)
    service.run_query(HUGE)
    rep = service.workload_report()
    gh = rep["guard_health"]
    assert gh["decisions"]["block"] == 1 and gh["block_rate"] == 0.5
    assert gh["check_ms"]["runs"] == 2 and gh["check_ms"]["p95"] >= gh["check_ms"]["p50"]
    assert rep["llm_usage"]["calls"] == 0
    assert all(r["shape_id"] for r in rep["workload_by_shape"])


def test_mcp_lists_five_tools_and_calls_them(service):
    from mcp import Client

    from cost_guard.mcp_server import build_server

    async def go():
        async with Client(build_server(service)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            res = await client.call_tool("check_query", {"sql": "SELECT 1"})
            return tools, res

    tools, res = asyncio.run(go())
    assert set(tools) == {"check_query", "run_query", "get_table_info", "get_query_history",
                          "workload_report", "registry_status"}
    assert tools["registry_status"].annotations.read_only_hint is True
    assert tools["check_query"].annotations.read_only_hint is True
    assert tools["run_query"].annotations.read_only_hint is False
    assert not res.is_error
    assert res.structured_content["decision"] == "allow"


def test_otel_export_nests_spans_and_shares_the_trace_id(service):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from cost_guard import observability

    exp = InMemorySpanExporter()
    assert observability.setup(exporter=exp, set_global=False)
    try:
        out = service.check_query(HUGE)
    finally:
        observability.shutdown()
    spans = exp.get_finished_spans()
    by_name = {s.name: s for s in spans}
    root, decide = by_name["mcp.check_query"], by_name["guard.decide"]
    assert format(root.context.trace_id, "032x") == out["trace_id"]
    assert decide.parent.span_id == root.context.span_id
    assert root.attributes["openinference.span.kind"] == "TOOL"
    assert root.attributes["decision"] == "block"
    exported = json.dumps([dict(s.attributes) for s in spans]).lower()
    assert "alice" not in exported and "users.email" not in exported
    assert not observability.enabled()


def test_caller_cannot_loosen_the_workload_class(service):
    # The loophole: the same block becomes a warn under 'scheduled' (10x caps).
    with pytest.raises(PermissionError, match="looser caps"):
        service.check_query(HUGE, workload_class="scheduled")
    with pytest.raises(PermissionError):
        service.run_query(HUGE, workload_class="scheduled")
    assert read_log(service.log_path) == []
    assert service.check_query(SMALL, workload_class="interactive")["workload_class"] == "interactive"


def test_stricter_class_is_allowed(synthetic_wh, tmp_path):
    svc = CostGuardService(synthetic_wh, load_customer(), tmp_path / "log.jsonl", None,
                           workload_class="scheduled")
    assert svc.check_query(SMALL, workload_class="interactive")["workload_class"] == "interactive"
    assert svc.check_query(SMALL)["workload_class"] == "scheduled"


def test_unknown_server_class_fails_at_startup(synthetic_wh):
    with pytest.raises(KeyError):
        CostGuardService(synthetic_wh, load_customer(), workload_class="nightly")


def test_mcp_has_no_bypass_and_returns_fix_hints(service):
    from mcp import Client

    from cost_guard.mcp_server import build_server

    async def go():
        async with Client(build_server(service)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            res = await client.call_tool("check_query", {"sql": HUGE})
            loose = await client.call_tool("run_query", {"sql": HUGE, "workload_class": "scheduled"})
            return tools, res, loose

    tools, res, loose = asyncio.run(go())
    assert "bypass_reason" not in tools["run_query"].input_schema["properties"]
    hints = res.structured_content["fix_hints"]
    assert hints and hints[0]["kind"] == "cause"
    assert loose.is_error and "looser caps" in loose.content[0].text


def test_registry_status_reports_freshness(service):
    out = service.registry_status()
    assert out["built"] and out["trace_id"]
    events = next(t for t in out["tables"] if t["table"] == "events")
    assert events["rows"] == 1_000_000 and not events["stale"]
    assert out["last_run"]["mode"] and "events" in out["last_run"]["profiled"]
