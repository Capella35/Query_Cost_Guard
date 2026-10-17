"""Fix hints on blocked verdicts: main cause first, fixes marked whether they
change the result, nothing on non-blocks, no change to any decision."""

from types import SimpleNamespace

from cost_guard.fix_hints import _unique
from cost_guard.guard import decide
from cost_guard.tracing import Tracer


def _check(sql, wh, **kw):
    return decide(sql, backend=wh, tracer=Tracer(), **kw)


def test_many_to_many_join_is_named_as_the_cause(synthetic_wh):
    # orders x events on user_id: both sides repeat user_id.
    v = _check("SELECT o.id, e.id FROM orders o JOIN events e ON o.user_id = e.user_id", synthetic_wh)
    assert v.decision == "block"
    causes = [h for h in v.fix_hints if h["kind"] == "cause"]
    assert any("many-to-many" in h["text"] and "user_id" in h["text"] for h in causes)
    fixes = [h for h in v.fix_hints if h["kind"] == "fix"]
    assert any(h["text"].startswith("aggregate") for h in fixes)
    assert all(h["changes_result"] is True for h in fixes)
    # Causes come before fixes.
    kinds = [h["kind"] for h in v.fix_hints]
    assert kinds.index("fix") > max(i for i, k in enumerate(kinds) if k == "cause")


def test_key_join_is_not_many_to_many(synthetic_wh):
    v = _check("SELECT * FROM orders o JOIN users u ON o.user_id = u.id", synthetic_wh)
    assert not any("many-to-many" in h["text"] for h in v.fix_hints)


def test_big_table_alone_and_date_filter_fix(synthetic_wh):
    v = _check("SELECT * FROM orders", synthetic_wh,
               customer={"workload_classes": {"interactive": {"max_rows_processed": 100_000},
                                              "scheduled": {"max_rows_processed": 10_000_000}},
                         "partitioned_tables": {"orders": "created_at"}})
    assert v.decision == "block"
    assert v.fix_hints[0]["kind"] == "cause" and "'orders' alone" in v.fix_hints[0]["text"]
    date_fix = next(h for h in v.fix_hints if "created_at" in h["text"])
    assert date_fix["changes_result"] is True
    info = [h for h in v.fix_hints if h["kind"] == "info"]
    assert info and "cannot raise" in info[0]["text"] and info[0]["changes_result"] is False


def test_no_hints_unless_blocked(synthetic_wh):
    v = _check("SELECT status, COUNT(*) FROM orders GROUP BY status", synthetic_wh)
    assert v.decision != "block" and v.fix_hints == []


def test_non_read_only_gets_one_hint(synthetic_wh):
    v = _check("DELETE FROM orders", synthetic_wh)
    assert len(v.fix_hints) == 1 and "read-only" in v.fix_hints[0]["text"]


def test_approximate_counts_get_a_looser_uniqueness_bar():
    # TheLook: orders.order_id is unique but HyperLogLog reads 0.916 of its rows.
    e = SimpleNamespace(total_rows=125226, null_count=0, distinct_count=114679, is_approximate=True)
    assert _unique(e)
    assert not _unique(SimpleNamespace(**{**vars(e), "is_approximate": False}))
