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


# ---- Frugal labeling: triage, cache, limit, review --------------------------------

import duckdb  # noqa: E402
import pytest  # noqa: E402

from cost_guard.column_kinds import (  # noqa: E402
    CLASSIFY_SYSTEM,
    classify_edge_cases,
    confirm,
    load_kinds,
    triage,
)


@pytest.mark.parametrize("name, dtype, kind", [
    ("user_id", "INTEGER", "identifier"), ("id", "BIGINT", "identifier"),
    ("sale_price", "FLOAT", "quantity"), ("num_of_item", "BIGINT", "quantity"),
    ("age", "INTEGER", "quantity"), ("latitude", "DOUBLE", "quantity"),
    ("amount_cents", "INTEGER", "quantity"), ("employee_count", "INTEGER", "quantity"),
    ("created_at", "TIMESTAMP", "date"), ("status", "VARCHAR", "not_numeric"),
    # edge cases: no clear signal, or an ambiguous ending
    ("sequence_number", "INTEGER", None), ("items_no", "INTEGER", None),
    ("zip_code", "INTEGER", None), ("customer", "INTEGER", None), ("stage", "INTEGER", None),
])
def test_triage(name, dtype, kind):
    assert triage(name, dtype).kind == kind


def test_thelook_numeric_columns_need_almost_no_model(thelook_wh):
    entries = thelook_wh.column_registry().entries()
    edge = [e.column_name for e in entries if triage(e.column_name, e.data_type).kind is None]
    assert edge == ["sequence_number"]  # 1 of 73


def test_general_prompt_names_no_test_column():
    for name in ("customer", "items_no", "visits_num", "amount_cents", "invoice", "ticket",
                 "owner", "agent", "zip", "paid_ymd", "sequence_number"):
        assert name not in CLASSIFY_SYSTEM


class _E:
    def __init__(self, t, c, dtype="INTEGER", rows=1000, ndv=500, nulls=0):
        self.table_name, self.column_name, self.data_type = t, c, dtype
        self.total_rows, self.distinct_count, self.null_count = rows, ndv, nulls


class _FakeLLM:
    model = "fake-model"

    def __init__(self, kind="identifier"):
        self.calls, self.seen, self.kind = 0, [], kind

    def classify_columns(self, tables):
        self.calls += 1
        self.seen.append(tables)
        return {(t, c["name"]): {"kind": self.kind, "reason": "r"}
                for t, s in tables.items() for c in s["columns"]}


def test_only_edge_cases_go_to_the_model_once_and_are_cached(tmp_path):
    path = str(tmp_path / "k.json")
    entries = [_E("sales", "user_id"), _E("sales", "price", "FLOAT"), _E("sales", "customer"),
               _E("sales", "items_no"), _E("sales", "note", "VARCHAR")]
    llm = _FakeLLM()
    rep = classify_edge_cases(entries, path, llm)
    assert rep.settled == 3 and rep.asked == ["sales.customer", "sales.items_no"] and llm.calls == 1
    cols = [c["name"] for c in llm.seen[0]["sales"]["columns"]]
    assert cols == ["customer", "items_no"]  # nothing else is sent
    assert load_kinds(path)["sales.customer"]["reviewed"] is False
    again = classify_edge_cases(entries, path, llm)
    assert again.cached == 2 and again.asked == [] and llm.calls == 1  # cached: no new call
    entries[2] = _E("sales", "customer", "VARCHAR(20)")  # type changed -> asked again
    entries[2].data_type = "BIGINT"
    assert classify_edge_cases(entries, path, llm).asked == ["sales.customer"]


def test_column_limit_and_dry_run(tmp_path):
    entries = [_E("t", f"c{i}") for i in range(5)]
    dry = classify_edge_cases(entries, str(tmp_path / "k.json"), llm=None, max_columns=3)
    assert len(dry.asked) == 3 and len(dry.over_limit) == 2 and not (tmp_path / "k.json").exists()
    llm = _FakeLLM()
    rep = classify_edge_cases(entries, str(tmp_path / "k.json"), llm, max_columns=3)
    assert llm.calls == 1 and sum(len(s["columns"]) for s in llm.seen[0].values()) == 3


def test_confirmed_labels_are_never_overwritten(tmp_path):
    path = str(tmp_path / "k.json")
    classify_edge_cases([_E("t", "items_no")], path, _FakeLLM("identifier"))
    confirm(path, "t.items_no", "quantity")
    llm = _FakeLLM("identifier")
    classify_edge_cases([_E("t", "items_no", "BIGINT")], path, llm)  # even after a type change
    lab = load_kinds(path)["t.items_no"]
    assert lab["kind"] == "quantity" and lab["reviewed"] and llm.calls == 0
    with pytest.raises(ValueError):
        confirm(path, "t.x", "number")


# ---- The SUM/AVG rule reads the labels -------------------------------------------

@pytest.fixture()
def shop(tmp_path):
    db = str(tmp_path / "shop.duckdb")
    con = duckdb.connect(db)
    con.execute("CREATE TABLE sales AS SELECT range AS sale_id, range % 900 AS customer, "
                "range % 7 AS items_no, range % 40 AS store_code, range % 50 AS qty FROM range(3000)")
    con.close()
    from cost_guard.backends.duckdb_backend import DuckDBWarehouse

    return DuckDBWarehouse(db)


def _verdict(wh, col, kinds):
    from cost_guard.guard import decide
    from cost_guard.tracing import Tracer

    wh._kinds = kinds
    return decide(f"SELECT SUM({col}) FROM sales", backend=wh, tracer=Tracer())


def test_levels(shop):
    assert _verdict(shop, "sale_id", {}).decision == "block"  # clear id name
    assert _verdict(shop, "qty", {}).decision == "allow"  # a quantity
    # ambiguous endings warn, never block
    for col in ("items_no", "store_code"):
        v = _verdict(shop, col, {})
        assert v.decision == "warn" and any("may aggregate" in f for f in v.findings)
    # a label saying quantity clears the ambiguous warning
    assert _verdict(shop, "items_no", {"sales.items_no": {"kind": "quantity"}}).decision == "allow"
    # a model's identifier label only warns until a person confirms it
    unconfirmed = {"sales.customer": {"kind": "identifier", "source": "m", "reviewed": False}}
    assert _verdict(shop, "customer", unconfirmed).decision == "warn"
    confirmed = {"sales.customer": {"kind": "identifier", "reviewed": True}}
    v = _verdict(shop, "customer", confirmed)
    assert v.decision == "block" and "confirmed identifier" in v.reason
    # a person can also clear a clear-looking id name (e.g. a quantity named *_key)
    assert _verdict(shop, "sale_id", {"sales.sale_id": {"kind": "quantity", "reviewed": True}}
                    ).decision == "allow"
