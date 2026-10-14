"""Tests for the tracer and its wiring into guard.decide()."""

from cost_guard.backends.base import PlanEstimate
from cost_guard.guard import decide
from cost_guard.tracing import Tracer


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
