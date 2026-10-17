"""Tests for the tracer, its wiring into guard.decide(), and what LLM spans record."""

import json
import threading

from cost_guard.backends.base import PlanEstimate
from cost_guard.guard import decide
from cost_guard.tracing import Tracer, annotate


class FakeBackend:
    def estimate(self, sql):
        return PlanEstimate(
            estimated_rows_scanned=10, estimated_bytes_scanned=None, detail="fake"
        )

    def table_metadata(self, table):
        return {
            "table": table,
            "num_rows": 10,
            "column_count": 3,
            "columns": ["id"],
        }

    def run_sample(self, sql, max_rows=10_000):
        return "fakehash"


def test_decide_emits_trace():
    t = Tracer()
    v = decide("SELECT id FROM users", backend=FakeBackend(), tracer=t)
    assert v.decision == "allow"
    names = [s["name"] for s in t.spans]
    assert "guard.decide" in names
    assert "estimate" in names
    assert any(n.startswith("rule.") for n in names)
    assert all(s["trace_id"] == t.spans[0]["trace_id"] for s in t.spans)
    assert all(isinstance(s["t_ms"], float) for s in t.spans)


def test_tracer_save_and_reset(tmp_path):
    t = Tracer()
    with t.trace("demo"):
        with t.span("x", "y"):
            pass
    p = tmp_path / "t.jsonl"
    assert t.save(p) == 2
    assert t.spans == []
    assert len(p.read_text().strip().split("\n")) == 2


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
