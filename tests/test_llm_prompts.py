"""LLM prompt inputs (fixed 2026-10-18 after the first Haiku eval): the
rewrite prompt names the dialect in use, and the explainer is told the
decision and its reason, not only the findings."""
from types import SimpleNamespace

from query_preflight import sql_ast
from query_preflight.guard import GuardVerdict, explain_inputs
from query_preflight.llm import ClaudeLLM


class Capture:
    def __init__(self):
        self.calls = []

    def parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason="end_turn", usage=None, parsed_output=SimpleNamespace(
            rewritten_sql="SELECT 1", preserves_results=False, explanation="x"))


def _llm():
    cap = Capture()
    return ClaudeLLM(client=SimpleNamespace(beta=SimpleNamespace(messages=cap)),
                     schema_lookup=lambda t: ["a"]), cap


def test_rewrite_prompt_names_the_dialect_in_use():
    llm, cap = _llm()
    with sql_ast.using_dialect("bigquery"):
        llm.propose_rewrite("SELECT a FROM t", ["finding"])
    system = cap.calls[-1]["system"]
    assert "BigQuery GoogleSQL" in system and "DuckDB" not in system
    assert "no MONTH or YEAR" in system  # P25: BigQuery rejects TIMESTAMP - INTERVAL 1 MONTH
    with sql_ast.using_dialect("duckdb"):
        llm.propose_rewrite("SELECT a FROM t", ["finding"])
    assert "DuckDB dialect" in cap.calls[-1]["system"] and "BigQuery" not in cap.calls[-1]["system"]


def test_explainer_is_told_the_reason_of_a_cap_block():
    """P01 / P08: blocked by a cap, so a reason and no findings."""
    v = GuardVerdict(decision="block", reason="estimated 201,614,937 bytes scanned exceeds the cap",
                     findings=[], fix_hints=[{"kind": "cause", "text": "c"},
                                             {"kind": "fix", "text": "add a created_at range"}])
    items = explain_inputs(v)
    assert items[0] == "decision: block; reason: estimated 201,614,937 bytes scanned exceeds the cap"
    assert items[1:] == ["suggested fix: add a created_at range"]
    # a warn's reason repeats its findings: they are not listed twice
    w = GuardVerdict(decision="warn", reason="no filter on created_at", findings=["no filter on created_at"])
    assert explain_inputs(w) == ["decision: warn; reason: no filter on created_at"]
    llm, cap = _llm()
    llm.explain_flag("SELECT * FROM events", items)
    assert "exceeds the cap" in cap.calls[-1]["messages"][0]["content"]
