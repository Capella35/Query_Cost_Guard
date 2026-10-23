"""Secrets never pass through Query Preflight: a query holding one is blocked before
the warehouse or the model sees it, and no tool response or log carries it."""
import json

from query_preflight import secret_scan
from query_preflight.guard import SECRET_FINDING, decide
from query_preflight.tracing import Tracer

FAKE_ANTHROPIC = "sk-ant-api03-" + "x" * 40
FAKE_GOOGLE = "AIza" + "B" * 35


def test_find_and_redact_known_formats(monkeypatch):
    text = f"key {FAKE_ANTHROPIC} and {FAKE_GOOGLE} and AKIAABCDEFGHIJKLMNOP"
    assert secret_scan.find(text) == ["Anthropic API key", "Google API key", "AWS access key id"]
    red = secret_scan.redact(text)
    assert FAKE_ANTHROPIC not in red and FAKE_GOOGLE not in red and "[REDACTED]" in red
    # ordinary SQL is not a secret: a column named token, a long id
    assert secret_scan.find("SELECT token FROM t WHERE token = 'abc123' AND id = 99999999") == []


def test_this_deployments_own_secret_values(monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_API_KEY", "plain-looking-value-123")
    assert secret_scan.find("WHERE x = 'plain-looking-value-123'") == ["the value of SOME_SERVICE_API_KEY"]
    assert "plain-looking" not in secret_scan.redact_obj({"a": ["plain-looking-value-123"]})["a"][0]
    monkeypatch.setenv("SHORT_KEY", "abc")  # too short to match safely: ignored
    assert secret_scan.find("WHERE x = 'abc'") == []


class Spy:
    """A backend and an LLM that record whether anything reached them."""
    name = "bigquery"

    def __init__(self):
        self.calls = []

    def __getattr__(self, attr):
        def call(*a, **k):
            self.calls.append(attr)
            raise AssertionError(f"{attr} must not be called for a query holding a secret")
        return call


def test_a_query_with_a_secret_is_blocked_before_the_warehouse_or_the_model():
    backend, llm = Spy(), Spy()
    v = decide(f"SELECT '{FAKE_ANTHROPIC}' AS k FROM users", backend=backend, tracer=Tracer(), llm=llm,
               bypass_reason="please")
    assert v.decision == "block" and v.findings == [SECRET_FINDING]
    assert "Anthropic API key" in v.reason and FAKE_ANTHROPIC not in v.reason
    assert backend.calls == [] and llm.calls == [] and v.bypassed is None
    assert v.explanation is None and v.suggestion is None and v.fix_hints == []


def test_mcp_never_returns_or_logs_a_secret(synthetic_wh, tmp_path):
    from query_preflight.config import load_customer
    from query_preflight.mcp_server import QueryPreflightService
    from query_preflight.runtime import read_log

    svc = QueryPreflightService(synthetic_wh, load_customer(), log_path=tmp_path / "log.jsonl",
                           trace_path=tmp_path / "t.jsonl", investigations=False)
    sql = f"SELECT id FROM users /* {FAKE_GOOGLE} */ WHERE id = 1"
    for out in (svc.check_query(sql), svc.run_query(sql)):
        assert out["decision"] == "block" and FAKE_GOOGLE not in json.dumps(out)
    assert svc.run_query(sql)["executed"] is False
    assert FAKE_GOOGLE not in (tmp_path / "log.jsonl").read_text()
    assert FAKE_GOOGLE not in (tmp_path / "t.jsonl").read_text()
    assert read_log(tmp_path / "log.jsonl")[-1]["status"] == "blocked"
