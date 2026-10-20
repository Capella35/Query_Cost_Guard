"""value_case: a filter value that matches a stored value only when case is
ignored is offered with the stored spelling (REWRITE); two stored values that
differ only by case are named (WARN), not guessed."""
from datetime import datetime, timedelta, timezone

from cost_guard import sql_ast
from cost_guard.column_registry import ColumnRegistry, RegistryEntry
from cost_guard.guard import decide
from cost_guard.tracing import Tracer
from cost_guard.value_case import fix_value_case


def _registry(values: dict[str, int], data_type="VARCHAR", age_days=0):
    at = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    total = sum(values.values())
    reg = ColumnRegistry([RegistryEntry(
        schema_name="main", table_name="orders", column_name="status", data_type=data_type,
        total_rows=total, distinct_count=len(values), null_count=0, is_approximate=False,
        profiled_at=at, min_value=None, max_value=None)])
    reg.add_value_counts([("main", "orders", "status", v, n) for v, n in values.items()])
    return reg


STATUS = {"Shipped": 30, "Cancelled": 20, "Returned": 10}


def _fix(sql, reg, max_age_days=35):
    return fix_value_case(sql_ast.parse_one(sql), reg, max_age_days)


def test_value_in_another_case_gets_the_stored_spelling():
    out, fixed, amb = _fix("SELECT COUNT(*) FROM orders WHERE status = 'CANCELLED'", _registry(STATUS))
    assert "status = 'Cancelled'" in out and amb == []
    assert "'Cancelled' does (20 rows" in fixed[0] and "result changes" in fixed[0]


def test_in_lists_literal_first_and_not_in_are_covered():
    out, fixed, _ = _fix("SELECT * FROM orders o WHERE 'shipped' = o.status "
                         "OR o.status NOT IN ('returned', 'Cancelled')", _registry(STATUS))
    assert "'Shipped' = o.status" in out and "NOT o.status IN ('Returned', 'Cancelled')" in out
    assert len(fixed) == 2


def test_left_alone_exact_unknown_wrapped_number_or_stale():
    reg = _registry(STATUS)
    for sql in ("SELECT * FROM orders WHERE status = 'Cancelled'",   # already stored
                "SELECT * FROM orders WHERE status = 'canceled'",    # no match even ignoring case
                "SELECT * FROM orders WHERE LOWER(status) = 'cancelled'"):  # not a bare column
        assert _fix(sql, reg) == (None, [], [])
    assert _fix("SELECT * FROM orders WHERE status = 'cancelled'",
                _registry(STATUS, data_type="INT64")) == (None, [], [])
    assert _fix("SELECT * FROM orders WHERE status = 'cancelled'",
                _registry(STATUS, age_days=40)) == (None, [], [])


def test_two_stored_values_that_differ_only_by_case_are_named_not_guessed():
    out, fixed, amb = _fix("SELECT * FROM orders WHERE status = 'cancelled'",
                           _registry({**STATUS, "CANCELLED": 5}))
    assert out is None and fixed == []
    assert "'CANCELLED' (5 rows) and 'Cancelled' (20 rows)" in amb[0]


def test_partial_value_counts_are_not_used():
    reg = _registry(STATUS)
    reg.lookup("orders", "status").total_rows += 1  # one value missing from the counts
    assert _fix("SELECT * FROM orders WHERE status = 'cancelled'", reg) == (None, [], [])


def test_guard_offers_the_corrected_query(synthetic_wh):
    # The synthetic warehouse stores 'cancelled' and 'complete' in lower case.
    v = decide("SELECT COUNT(*) FROM orders WHERE status = 'Cancelled'",
               backend=synthetic_wh, tracer=Tracer())
    assert v.decision == "rewrite" and v.rewrite_source == "value_case"
    assert "status = 'cancelled'" in v.rewritten_sql
    v = decide("SELECT COUNT(*) FROM orders WHERE status = 'cancelled'",
               backend=synthetic_wh, tracer=Tracer())
    assert v.rewrite_source != "value_case" and not any("stored value" in f for f in v.findings)
