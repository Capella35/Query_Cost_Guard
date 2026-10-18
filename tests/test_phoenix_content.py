"""Phoenix export modes: numbers only (default) and content (evals on
TheLook). Content reaches the EXPORTED spans only, never the JSONL trace."""
import argparse
import sys
from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from cost_guard import observability  # noqa: E402
from cost_guard.tracing import Tracer  # noqa: E402

SQL = "SELECT email FROM users WHERE email = 'alice@example.com'"


@pytest.fixture
def export():
    def start(content):
        exporter = InMemorySpanExporter()
        assert observability.setup(exporter=exporter, set_global=False,
                                   project="p", content=content)
        return exporter
    yield start
    observability.shutdown()


def _run(tmp_path):
    tracer = Tracer()
    with tracer.trace("llm.explain_flag", findings=2):
        observability.add_content(**{"input.value": SQL, "output.value": "warn"})
    path = tmp_path / "t.jsonl"
    tracer.save(path)
    return path.read_text()


def test_numbers_only_exports_no_content(export, tmp_path):
    exporter = export(False)
    jsonl = _run(tmp_path)
    (span,) = exporter.get_finished_spans()
    assert "input.value" not in span.attributes and span.attributes["findings"] == 2
    assert span.resource.attributes["cost_guard.content"] == "none"
    assert "alice" not in jsonl


def test_content_mode_exports_sql_but_never_writes_it_to_jsonl(export, tmp_path):
    exporter = export(True)
    jsonl = _run(tmp_path)
    (span,) = exporter.get_finished_spans()
    assert span.attributes["input.value"] == SQL
    assert span.resource.attributes["cost_guard.content"] == "included"
    assert span.resource.attributes["openinference.project.name"] == "p"
    assert "alice" not in jsonl and "input.value" not in jsonl


def test_add_content_is_a_no_op_when_export_is_off():
    observability.shutdown()
    observability.add_content(**{"input.value": SQL})  # nothing to attach to; must not raise
    assert not observability.content_enabled()


def test_run_eval_guards_the_content_flag():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
    import run_eval

    ns = lambda **kw: argparse.Namespace(**{"phoenix": False, "phoenix_content": False,  # noqa: E731
                                            "suite": "bq-validation",
                                            "dataset": "cost_guard_thelook", **kw})
    assert run_eval._phoenix_setup(ns()) is False
    with pytest.raises(SystemExit, match="needs --phoenix"):
        run_eval._phoenix_setup(ns(phoenix_content=True))
    with pytest.raises(SystemExit, match="TheLook evals only"):
        run_eval._phoenix_setup(ns(phoenix=True, phoenix_content=True, dataset="sales"))
