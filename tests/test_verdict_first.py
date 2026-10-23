"""Verdict first (owner's principle, DESIGN.md §12, 2026-10-23): the
decision comes from decide(), which takes no model; the model's help comes
after, from assist(), only on warn and block, and never changes the
decision or its reason. Before the split, a verified model rewrite turned a
warn into "rewrite" inside the verdict step."""
import inspect
import json
import threading

import pytest

from query_preflight.guard import assist, decide
from query_preflight.interfaces import RewriteProposal
from query_preflight.tracing import Tracer

HAVING = ("SELECT status, COUNT(*) AS n FROM orders WHERE created_at >= '2024-01-01' "
          "GROUP BY status HAVING status <> 'cancelled'")
GOOD = ("SELECT status, COUNT(*) AS n FROM orders WHERE created_at >= '2024-01-01' "
        "AND status <> 'cancelled' GROUP BY status")
WRAPPED = ("SELECT order_id, status FROM orders "
           "WHERE DATE_TRUNC('month', created_at) = DATE '2024-01-01'")
CLEAN = "SELECT id FROM users WHERE id = 1"
CARTESIAN = "SELECT * FROM users, orders"


class RewritingLLM:
    """Proposes a correct, cheaper rewrite: the strongest pull on the verdict."""

    def __init__(self):
        self.calls = 0

    def propose_rewrite(self, sql, findings):
        self.calls += 1
        return RewriteProposal(sql, GOOD, "fake")

    def explain_flag(self, sql, findings):
        self.calls += 1
        return "explanation"


def test_decide_takes_no_model():
    assert "llm" not in inspect.signature(decide).parameters


@pytest.mark.parametrize("sql, decision", [(CLEAN, "allow"), (HAVING, "warn"),
                                           (WRAPPED, "rewrite"), (CARTESIAN, "block")])
def test_the_model_never_changes_the_verdict(synthetic_wh, sql, decision):
    v = decide(sql, backend=synthetic_wh, tracer=Tracer())
    before = (v.decision, v.reason, v.rewritten_sql, list(v.findings))
    llm = RewritingLLM()
    assist(sql, v, llm, backend=synthetic_wh, tracer=Tracer())
    assert v.decision == decision
    assert (v.decision, v.reason, v.rewritten_sql, list(v.findings)) == before
    if decision in ("allow", "rewrite"):
        assert llm.calls == 0  # the model only helps where the verdict failed
    else:
        assert v.explanation == "explanation"


def test_a_verified_model_rewrite_is_an_offer_not_a_decision(synthetic_wh):
    v = assist(HAVING, decide(HAVING, backend=synthetic_wh, tracer=Tracer()), RewritingLLM(),
               backend=synthetic_wh, tracer=Tracer())
    assert v.decision == "warn" and v.rewritten_sql is None
    assert v.suggestion["verified_rewrite"] and v.suggestion["sql"] == GOOD


def test_mcp_returns_the_verdict_before_the_model_finishes(synthetic_wh, tmp_path):
    from query_preflight.config import load_customer
    from query_preflight.mcp_server import QueryPreflightService

    release = threading.Event()

    class SlowLLM(RewritingLLM):
        def explain_flag(self, sql, findings):
            release.wait(10)
            return "late explanation"

    svc = QueryPreflightService(synthetic_wh, load_customer(), log_path=tmp_path / "log.jsonl",
                                trace_path=tmp_path / "t.jsonl", investigations=False,
                                llm=SlowLLM())
    out = svc.check_query(HAVING)  # returns while the model is still blocked
    assert out["decision"] == "warn" and out["explanation"] is None
    assert out["assist"]["status"] == "pending"
    aid = out["assist"]["assist_id"]
    assert svc.get_assistance(aid, wait_sec=0)["status"] == "pending"
    release.set()
    ready = svc.get_assistance(aid, wait_sec=10)
    assert ready["status"] == "ready" and ready["explanation"] == "late explanation"
    assert ready["suggestion"]["verified_rewrite"]
    assert svc.check_query(CLEAN)["assist"] is None  # nothing to help with
    with pytest.raises(KeyError):
        svc.get_assistance("nope")


def test_cli_prints_the_verdict_line_first(synthetic_wh, monkeypatch, capsys):
    from query_preflight import __main__ as cli

    monkeypatch.setattr(cli, "_llm", lambda args, backend: RewritingLLM())
    assert cli.main(["--db", synthetic_wh.db_path, "check", HAVING, "--llm", "claude"]) == 0
    first, second = capsys.readouterr().out.strip().split("\n")
    assert json.loads(first)["decision"] == "warn"
    assert json.loads(second)["assist"]["suggestion"]["verified_rewrite"]
