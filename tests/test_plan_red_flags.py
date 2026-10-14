"""Plan red flags (full in-memory sorts, from EXPLAIN) and long IN lists."""
import pytest

from cost_guard import sql_ast
from cost_guard.guard import decide
from cost_guard.plan_estimate import plan_cardinality
from cost_guard.rules.static_rules import MAX_IN_LIST_ITEMS, no_long_in_list
from cost_guard.tracing import Tracer

SORT_MSG = "full in-memory sort"


def _sort_plan(name, rows):
    scan = {"name": "SEQ_SCAN", "children": [],
            "extra_info": {"Table": "db.main.events", "Projections": ["id"],
                           "Estimated Cardinality": str(rows)}}
    return [{"name": "PROJECTION", "extra_info": {"Estimated Cardinality": "0"},
             "children": [{"name": name, "extra_info": {}, "children": [scan]}]}]


def test_plan_records_full_sorts_only():
    assert plan_cardinality(_sort_plan("ORDER_BY", 2_000_000)).sorts == [2_000_000]
    assert plan_cardinality(_sort_plan("TOP_N", 2_000_000)).sorts == []


@pytest.mark.parametrize("sql", [
    "SELECT id, created_at FROM events WHERE created_at >= '2024-01-01' ORDER BY created_at",
    "SELECT * FROM (SELECT id, created_at FROM events WHERE created_at >= '2024-01-01' "
    "ORDER BY created_at) t",
])
def test_large_full_sort_flagged(synthetic_wh, sql):
    v = decide(sql, backend=synthetic_wh, tracer=Tracer())
    assert v.decision == "warn"
    assert any(SORT_MSG in f for f in v.findings)


@pytest.mark.parametrize("sql", [
    # LIMIT: planned as TOP_N, keeps only the top rows
    "SELECT id, created_at FROM events WHERE created_at >= '2024-01-01' ORDER BY created_at LIMIT 100",
    # sort after GROUP BY: sorts the groups, not the table
    "SELECT status, COUNT(*) AS n FROM orders WHERE created_at >= '2024-01-01' "
    "GROUP BY status ORDER BY n DESC",
    # a window's ORDER BY is a WINDOW operator, not a result sort
    "SELECT id, ROW_NUMBER() OVER (ORDER BY created_at) FROM events WHERE created_at >= '2024-01-01'",
])
def test_bounded_or_small_sort_not_flagged(synthetic_wh, sql):
    v = decide(sql, backend=synthetic_wh, tracer=Tracer())
    assert not any(SORT_MSG in f for f in v.findings)


def test_sort_check_visibly_skipped_without_plan():
    v = decide("SELECT id FROM events ORDER BY created_at", backend=None, tracer=Tracer())
    assert any(s.startswith("plan_unbounded_sort:") for s in v.skipped_rules)


def test_thelook_unbounded_sort(thelook_wh):
    """H14's shape, with a date filter so only the sort can flag it."""
    v = decide("SELECT id, uri, created_at FROM events WHERE created_at >= TIMESTAMP '2019-01-01' "
               "ORDER BY created_at DESC", backend=thelook_wh, tracer=Tracer())
    assert any(SORT_MSG in f for f in v.findings)


def _in_list(n, neg=False):
    vals = ", ".join(str(i) for i in range(n))
    return f"SELECT id FROM users WHERE id {'NOT ' if neg else ''}IN ({vals})"


@pytest.mark.parametrize("sql", [_in_list(MAX_IN_LIST_ITEMS + 1), _in_list(500, neg=True)])
def test_long_in_list(sql):
    findings = no_long_in_list(sql, {})
    assert len(findings) == 1 and "explicit values" in findings[0]


@pytest.mark.parametrize("sql", [
    _in_list(MAX_IN_LIST_ITEMS),
    "SELECT id FROM users WHERE id IN (SELECT user_id FROM orders)",
])
def test_short_or_subquery_in_not_flagged(sql):
    assert no_long_in_list(sql, {}) == []


def test_guard_warns_on_long_in_list(synthetic_wh):
    v = decide(_in_list(300), backend=synthetic_wh, tracer=Tracer())
    assert v.decision == "warn"
    assert any("explicit values" in f for f in v.findings)
    assert sql_ast.long_in_lists(sql_ast.parse_one(_in_list(300)), MAX_IN_LIST_ITEMS) == [("id", 300)]
