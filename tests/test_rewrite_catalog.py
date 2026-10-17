"""Deterministic rewrite catalog: sargable date ranges and filter pushdown.
Every rewrite must return the same rows as the original — checked here on
the full synthetic data, not just the Guard's sample."""
import pytest

from cost_guard import sql_ast
from cost_guard.guard import decide
from cost_guard.results import result_hash
from cost_guard.rewrites import suggest_pushdown_rewrite, suggest_sargable_rewrite
from cost_guard.tracing import Tracer


def _same(wh, a, b):
    con = wh._connect()
    ordered = sql_ast.has_order_by(sql_ast.parse_one(a))
    return (result_hash(con.execute(a).fetchall(), ordered)
            == result_hash(con.execute(b).fetchall(), ordered))


@pytest.mark.parametrize("sql", [
    "SELECT order_id FROM orders WHERE DATE_TRUNC('month', created_at) = DATE '2024-02-01'",
    "SELECT order_id FROM orders WHERE DATE_TRUNC('month', created_at) = '2024-02-01'",
    "SELECT order_id FROM orders WHERE DATE_TRUNC('year', created_at) = DATE '2023-01-01' AND status = 'complete'",
    "SELECT order_id FROM orders WHERE CAST(created_at AS DATE) = DATE '2024-03-15'",
    "SELECT order_id FROM orders WHERE created_at::DATE = '2024-03-15'",
    # Not month-aligned: the original matches nothing; so must the rewrite.
    "SELECT order_id FROM orders WHERE DATE_TRUNC('month', created_at) = DATE '2024-02-10'",
])
def test_sargable_rewrite_is_equivalent(synthetic_wh, sql):
    out = suggest_sargable_rewrite(sql)
    assert out is not None and "DATE_TRUNC('MONTH', created_at)" not in out
    assert _same(synthetic_wh, sql, out)


@pytest.mark.parametrize("sql", [
    "SELECT order_id FROM orders WHERE DATE_TRUNC('month', created_at) > DATE '2024-02-01'",  # not =
    "SELECT order_id FROM orders WHERE EXTRACT(month FROM created_at) = 6",  # no single range
    "SELECT order_id FROM orders WHERE created_at >= '2024-01-01'",  # already sargable
])
def test_sargable_leaves_other_shapes_alone(sql):
    assert suggest_sargable_rewrite(sql) is None


PUSH = ("SELECT t.order_id, t.country FROM (SELECT o.order_id, o.created_at, u.country "
        "FROM orders o JOIN users u ON u.id = o.user_id) t "
        "WHERE t.created_at >= '2024-06-01' AND t.country = 'US'")


def test_pushdown_rewrite_is_equivalent(synthetic_wh):
    out = suggest_pushdown_rewrite(PUSH)
    assert out is not None
    inner = sql_ast.parse_one(out).find(sql_ast.exp.Subquery).this
    assert inner.args.get("where") is not None
    assert sql_ast.parse_one(out).args.get("where") is None  # all pushed
    assert _same(synthetic_wh, PUSH, out)


@pytest.mark.parametrize("sql", [
    # Aggregated / limited derived tables: pushing a filter in changes results.
    "SELECT t.user_id FROM (SELECT o.user_id, COUNT(*) AS n FROM orders o JOIN users u "
    "ON u.id = o.user_id GROUP BY o.user_id) t WHERE t.n > 5",
    "SELECT t.order_id FROM (SELECT o.order_id, u.country FROM orders o JOIN users u "
    "ON u.id = o.user_id LIMIT 10) t WHERE t.country = 'US'",
    # Outer query joins another relation: not pushed.
    "SELECT t.order_id FROM (SELECT o.order_id, u.country FROM orders o JOIN users u "
    "ON u.id = o.user_id) t JOIN users x ON x.id = 1 WHERE t.country = 'US'",
])
def test_pushdown_refuses_unsafe_shapes(sql):
    assert suggest_pushdown_rewrite(sql) is None


def test_select_star_and_missing_filter_get_no_rewrite(synthetic_wh):
    """No equivalent rewrite exists: dropping columns or adding a date
    filter changes the result."""
    for q in ("SELECT * FROM events WHERE created_at >= '2023-06-01'",  # ~79% of the 1M cap
              "SELECT event_type, COUNT(*) FROM events GROUP BY event_type"):
        v = decide(q, backend=synthetic_wh, tracer=Tracer())
        assert v.decision == "warn" and v.rewritten_sql is None


def test_cast_date_rewrite_not_held_to_estimate_gate(synthetic_wh):
    """DuckDB estimates `CAST(col AS DATE) = X` from the distinct count (~1k rows)
    and the equivalent range at a flat 20% (200k), so the estimate gate used to
    reject this rewrite. A pruning-only fix reads no more than the original;
    compare_results still has to pass."""
    from cost_guard.guard import decide
    from cost_guard.tracing import Tracer

    q = "SELECT order_id FROM orders WHERE CAST(created_at AS DATE) = DATE '2024-03-01'"
    v = decide(q, backend=synthetic_wh, tracer=Tracer())
    assert v.decision == "rewrite" and v.rewrite_source == "deterministic:sargable"
    assert "prunable form" in v.reason
    assert not any("not cheaper by estimate" in f for f in v.findings)


@pytest.mark.parametrize("sql", [
    "SELECT order_id FROM orders WHERE EXTRACT(year FROM created_at) = 2024",
    "SELECT order_id FROM orders WHERE EXTRACT(year FROM created_at) = 2023 AND status = 'Complete'",
    "SELECT order_id FROM orders WHERE created_at + INTERVAL 1 DAY >= TIMESTAMP '2024-03-02'",
    "SELECT order_id FROM orders WHERE created_at - INTERVAL 6 HOUR < TIMESTAMP '2024-03-02 12:00:00'",
    "SELECT order_id FROM orders WHERE TIMESTAMP '2024-03-02' <= created_at + INTERVAL 2 DAY",
])
def test_wider_sargable_rewrites_are_equivalent(synthetic_wh, sql):
    """EXTRACT(YEAR ...) = N and col +/- INTERVAL (fixed units) become a range
    on the bare column; same rows on the full synthetic data."""
    out = suggest_sargable_rewrite(sql)
    assert out is not None and "EXTRACT" not in out.upper() and "INTERVAL" not in out.upper()
    assert _same(synthetic_wh, sql, out)


@pytest.mark.parametrize("sql", [
    "SELECT order_id FROM orders WHERE created_at + INTERVAL 1 MONTH >= TIMESTAMP '2024-03-02'",  # not invertible
    "SELECT order_id FROM orders WHERE EXTRACT(day FROM created_at) = 3",
])
def test_wider_sargable_leaves_inexact_shapes_alone(sql):
    assert suggest_sargable_rewrite(sql) is None


def test_format_timestamp_ranges_in_bigquery_sql():
    """FORMAT_TIMESTAMP is BigQuery-only, so this checks the generated SQL (the
    live dry runs showed 2.77 -> 0.01 MiB on order_items); the literal must
    match the format exactly, else no rewrite."""
    with sql_ast.using_dialect("bigquery"):
        out = suggest_sargable_rewrite(
            "SELECT order_id FROM order_items WHERE FORMAT_TIMESTAMP('%Y-%m', created_at) = '2023-12'")
        assert "CAST('2023-12-01 00:00:00' AS TIMESTAMP)" in out
        assert "CAST('2024-01-01 00:00:00' AS TIMESTAMP)" in out and "DATETIME" not in out
        assert suggest_sargable_rewrite(
            "SELECT 1 FROM order_items WHERE FORMAT_TIMESTAMP('%Y-%m', created_at) = '2023-6'") is None


@pytest.mark.parametrize("sql", [
    "SELECT order_id FROM orders WHERE EXTRACT(year FROM created_at) = 2024 AND EXTRACT(month FROM created_at) = 2",
    "SELECT order_id FROM orders WHERE EXTRACT(month FROM created_at) = 12 AND status = 'complete' "
    "AND EXTRACT(year FROM created_at) = 2023",
    "SELECT order_id FROM orders WHERE 2024 = EXTRACT(year FROM created_at) AND 3 = EXTRACT(month FROM created_at) "
    "AND EXTRACT(day FROM created_at) = 15",
    # Impossible dates match nothing; so must the rewrite.
    "SELECT order_id FROM orders WHERE EXTRACT(year FROM created_at) = 2024 AND EXTRACT(month FROM created_at) = 13",
    "SELECT order_id FROM orders WHERE EXTRACT(year FROM created_at) = 2023 AND EXTRACT(month FROM created_at) = 2 "
    "AND EXTRACT(day FROM created_at) = 30",
])
def test_year_and_month_combine_into_one_range(synthetic_wh, sql):
    """T15: EXTRACT(YEAR) = Y AND EXTRACT(MONTH) = M (and DAY) on one column is
    one range on the bare column; same rows on the full synthetic data."""
    out = suggest_sargable_rewrite(sql)
    assert out is not None and "EXTRACT(YEAR" not in out.upper()
    assert _same(synthetic_wh, sql, out)


def test_year_and_month_combine_in_bigquery_sql():
    with sql_ast.using_dialect("bigquery"):
        out = suggest_sargable_rewrite(
            "SELECT order_id, status FROM orders WHERE EXTRACT(YEAR FROM created_at) = 2025 "
            "AND EXTRACT(MONTH FROM created_at) = 6")
    assert "EXTRACT" not in out
    assert "created_at >= CAST('2025-06-01 00:00:00' AS TIMESTAMP)" in out
    assert "created_at < CAST('2025-07-01 00:00:00' AS TIMESTAMP)" in out


@pytest.mark.parametrize("sql", [
    # Different columns: no single range for either month.
    "SELECT o.order_id FROM orders o JOIN users u ON u.id = o.user_id "
    "WHERE EXTRACT(year FROM o.created_at) = 2024 AND EXTRACT(month FROM u.created_at) = 2",
    # OR: not a conjunction.
    "SELECT order_id FROM orders WHERE EXTRACT(year FROM created_at) = 2024 OR EXTRACT(month FROM created_at) = 2",
])
def test_month_without_its_year_is_not_combined(sql):
    out = suggest_sargable_rewrite(sql)
    assert out is None or "EXTRACT(MONTH" in out.upper()
