"""MCP server: tool outputs, boundaries (no rows, run is the only path, read-only),
one trace per call, and the optional OpenTelemetry/Phoenix export."""

import asyncio
import json
import threading

import pytest

from cost_guard.config import load_customer
from cost_guard.mcp_server import CostGuardService, shape_id
from cost_guard.runtime import read_log
from cost_guard.tracing import Tracer, annotate, read_traces

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
    out = service.run_query("DELETE FROM orders", bypass_reason="please")
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
                          "workload_report"}
    assert tools["check_query"].annotations.read_only_hint is True
    assert tools["run_query"].annotations.read_only_hint is False
    assert not res.is_error
    assert res.structured_content["decision"] == "allow"


def test_tracer_is_thread_safe():
    t = Tracer()

    def work():
        for _ in range(200):
            with t.trace("demo"):
                with t.span(None, "child"):
                    pass

    threads = [threading.Thread(target=work) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(t.spans) == 8 * 200 * 2
    roots = {s["span_id"]: s for s in t.spans if s["name"] == "demo"}
    # Each thread's child nests under its own root, never another thread's.
    for s in t.spans:
        if s["name"] == "child":
            assert roots[s["parent_id"]]["trace_id"] == s["trace_id"]


def test_annotate_adds_to_current_span():
    t = Tracer()
    with t.trace("x"):
        annotate(input_tokens=5)
    annotate(ignored=True)  # no current span: a no-op
    assert t.spans[0]["attrs"] == {"input_tokens": 5}


def test_llm_records_token_counts_not_prompts():
    from types import SimpleNamespace

    from cost_guard.llm import ClaudeLLM

    class FakeMessages:
        def parse(self, **kw):
            return SimpleNamespace(stop_reason="end_turn", parsed_output=SimpleNamespace(
                explanation="scans everything"),
                usage=SimpleNamespace(input_tokens=120, output_tokens=30, cache_read_input_tokens=0))

    llm = ClaudeLLM(client=SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages())))
    t = Tracer()
    with t.trace("llm.explain_flag"):
        llm.explain_flag("SELECT secret FROM t", ["full scan"])
    attrs = t.spans[0]["attrs"]
    assert attrs["input_tokens"] == 120 and attrs["output_tokens"] == 30
    assert "secret" not in json.dumps(attrs)


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
