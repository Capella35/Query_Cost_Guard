"""column_kinds: the baseline, the scoring fixed in eval/column_kinds.yaml, and
the model call (fake client — no API)."""
from types import SimpleNamespace

from cost_guard.column_kinds import baseline_kind, describe_tables, score
from cost_guard.llm import ClaudeLLM, ColumnKindOut, ColumnKindsOut

TABLES = {"invoices": {"rows": 1000, "columns": [
    {"name": "customer", "type": "INTEGER", "distinct": 80, "null_share": 0.0, "kind": "identifier"},
    {"name": "items_no", "type": "INTEGER", "distinct": 20, "null_share": 0.0, "kind": "quantity"},
    {"name": "zip", "type": "INTEGER", "distinct": 90, "null_share": 0.0, "kind": "category",
     "also_ok": "identifier"},
    {"name": "note", "type": "STRING", "distinct": 990, "null_share": 0.1, "kind": "text"},
]}}


def test_baseline_is_the_name_pattern_then_the_type():
    assert baseline_kind("user_id", "INTEGER", 100, 90) == "identifier"
    assert baseline_kind("items_no", "INTEGER", 100, 5) == "identifier"  # the pattern's false id
    assert baseline_kind("customer", "INTEGER", 100, 90) == "quantity"  # its missed id
    assert baseline_kind("created", "TIMESTAMP", 100, 90) == "date"
    assert baseline_kind("status", "STRING", 100, 5) == "category"
    assert baseline_kind("note", "STRING", 100, 90) == "text"


def test_score_counts_false_and_missed_ids_on_numeric_columns():
    base = {(t, c["name"]): baseline_kind(c["name"], c["type"], s["rows"], c["distinct"])
            for t, s in TABLES.items() for c in s["columns"]}
    r = score(TABLES, base)
    assert r["false_ids"] == ["invoices.items_no"] and r["missed_ids"] == ["invoices.customer"]
    perfect = score(TABLES, {("invoices", "customer"): "identifier", ("invoices", "items_no"): "quantity",
                             ("invoices", "zip"): "identifier", ("invoices", "note"): "text"})
    assert perfect["accuracy"] == "4/4" and not perfect["false_ids"] and not perfect["missed_ids"]
    missing = score(TABLES, {})
    assert missing["accuracy"] == "0/4" and missing["missed_ids"] == ["invoices.customer"]


def test_prompt_has_counts_but_no_values():
    text = describe_tables(TABLES)
    assert "customer INTEGER, 80 distinct, 0.0% NULL" in text and "1,000 rows" in text
    assert "kind" not in text and "identifier" not in text  # no labels leak into the prompt


def test_classify_columns_with_a_fake_client():
    seen = {}

    class Beta:
        def parse(self, **kw):
            seen.update(kw)
            return SimpleNamespace(stop_reason="end_turn", parsed_output=ColumnKindsOut(columns=[
                ColumnKindOut(table="invoices", column="customer", kind="identifier", reason="fk")]))

    llm = ClaudeLLM(client=SimpleNamespace(beta=SimpleNamespace(messages=Beta())))
    out = llm.classify_columns(TABLES)
    assert out == {("invoices", "customer"): {"kind": "identifier", "reason": "fk"}}
    assert seen["output_format"] is ColumnKindsOut and "items_no INTEGER" in seen["messages"][0]["content"]
