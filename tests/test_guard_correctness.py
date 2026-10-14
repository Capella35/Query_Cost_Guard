"""Phase 1 acceptance tests: F1.2 cartesian logic, F1.3 fan-out estimates,
F1.4 bounded verification, F1.5 read-only classifier."""
import pytest

from cost_guard import sql_ast
from cost_guard.guard import decide, is_read_only
from cost_guard.tools.compare_results import compare_results
from cost_guard.tracing import Tracer


def _decide(sql, wh, **kw):
    return decide(sql, backend=wh, tracer=Tracer(), **kw)


# ---- F1.2 -------------------------------------------------------------------

def test_cross_join_with_linking_where_is_allowed(synthetic_wh):
    v = _decide(
        "SELECT u.name, o.order_id FROM users u CROSS JOIN orders o "
        "WHERE u.id = o.user_id AND o.created_at >= '2024-01-01'",
        synthetic_wh,
    )
    assert v.decision == "allow", v.reason
    assert not any("cartesian" in f for f in v.findings)


def test_cross_join_with_one_row_aggregate_cte_is_allowed(synthetic_wh):
    v = _decide(
        "WITH t AS (SELECT SUM(amount_cents) AS total FROM orders "
        "WHERE created_at >= '2024-01-01') "
        "SELECT o.status, SUM(o.amount_cents) * 1.0 / t.total FROM orders o "
        "CROSS JOIN t WHERE o.created_at >= '2024-01-01' GROUP BY o.status, t.total",
        synthetic_wh,
    )
    assert v.decision == "allow", v.reason


def test_comma_join_with_unrelated_filter_is_blocked(synthetic_wh):
    v = _decide("SELECT u.name, o.order_id FROM users u, orders o WHERE u.age_band = 3",
                synthetic_wh)
    assert v.decision == "block"
    assert any(f.startswith("cartesian") for f in v.findings), v.findings


def test_cross_join_to_tiny_table_is_exempt():
    tree = sql_ast.parse_one("SELECT * FROM orders o CROSS JOIN distribution_centers d")
    assert sql_ast.cartesian_findings(tree, {"orders": 125_226, "distribution_centers": 10}) == []
    assert sql_ast.cartesian_findings(tree, {"orders": 125_226, "distribution_centers": 50_000})


def test_unqualified_join_predicate_is_not_flagged():
    tree = sql_ast.parse_one("SELECT * FROM users, orders WHERE id = user_id")
    assert sql_ast.cartesian_findings(tree) == []


# ---- F1.3 -------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT a.user_id FROM events a JOIN events b ON a.user_id = b.user_id",
    "SELECT * FROM users, orders",
])
def test_fanout_exceeds_cap(synthetic_wh, sql):
    est = synthetic_wh.estimate(sql)
    assert est.source == "explain"
    # The old table-sum measure stays under the 5M interactive cap...
    table_sum = sum(synthetic_wh.row_count(t) for t in sql_ast.referenced_tables(sql_ast.parse_one(sql)))
    assert table_sum < 5_000_000
    # ...the plan's fan-out does not.
    assert est.rows_processed > 5_000_000
    assert _decide(sql, synthetic_wh).decision == "block"


def test_estimate_fallback_is_labeled(synthetic_wh):
    est = synthetic_wh.estimate("SELECT no_such_column FROM orders")
    assert est.source == "table_sum_fallback"
    assert "FALLBACK" in est.detail


# ---- F1.4 -------------------------------------------------------------------

def test_unordered_but_equal_results_pass(synthetic_wh):
    a = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
    b = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY status DESC"
    assert compare_results(a, b, backend=synthetic_wh).passed


def test_float_tolerance_and_nulls(synthetic_wh):
    a = "SELECT 0.1 + 0.2 AS x, NULL AS y"
    b = "SELECT 0.3 AS x, NULL AS y"
    # Literal-only queries read no table: nothing to sample, still compared.
    assert compare_results(a, b, backend=synthetic_wh).passed
    assert not compare_results(a, "SELECT 0.3 AS x, 0 AS y", backend=synthetic_wh).passed


def test_wrong_rewrite_is_rejected(synthetic_wh):
    a = "SELECT status, COUNT(*) FROM orders GROUP BY status"
    b = "SELECT status, COUNT(*) FROM orders WHERE amount_cents > 100 GROUP BY status"
    assert not compare_results(a, b, backend=synthetic_wh).passed


def test_heavy_query_verification_is_bounded(synthetic_wh):
    """Stated bound: verification reads at most 10% of the full input rows
    (50k-row sample per table vs 1M-row orders)."""
    heavy = ("SELECT user_id, COUNT(*), SUM(amount_cents), AVG(amount_cents) "
             "FROM orders GROUP BY user_id")
    res = compare_results(heavy, heavy + " ORDER BY user_id", backend=synthetic_wh)
    assert res.passed, res.detail
    assert res.input_rows_full == 1_000_000
    assert res.input_rows_sampled / res.input_rows_full <= 0.10


def test_verification_timeout_fails_closed(synthetic_wh):
    slow = "SELECT COUNT(*), SUM(a.id * b.id) FROM users a, events b"
    res = compare_results(slow, slow, backend=synthetic_wh, sample_rows=1_000_000,
                          timeout_sec=0.2)
    assert not res.passed
    assert "timeout" in res.detail


def test_empty_vs_empty_is_inconclusive(synthetic_wh):
    q = "SELECT order_id FROM orders WHERE amount_cents < 0"
    res = compare_results(q, q, backend=synthetic_wh)
    assert not res.passed and "inconclusive" in res.detail


# ---- F1.5 -------------------------------------------------------------------

def test_keyword_inside_string_literal_is_read_only():
    assert is_read_only("SELECT * FROM orders WHERE status = 'create'")
    assert is_read_only("SELECT id FROM users WHERE name = 'drop table users'")
    assert is_read_only("SELECT created_at, updated_by FROM orders")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_text('/etc/hostname')",
    "SELECT * FROM read_csv('x.csv')",
    "SELECT * FROM glob('*')",
    "SELECT * FROM 'data/raw/users.csv'",
    "SELECT getenv('HOME')",
    "SELECT 1; DROP TABLE users",
    "SELECT 1; SELECT 2",
    "DELETE FROM users",
    "ATTACH 'other.db'",
    "COPY orders TO 'out.csv'",
    "SELEC broken",
    "",
])
def test_not_read_only(sql):
    assert not is_read_only(sql)


def test_value_generators_allowed():
    assert is_read_only("SELECT * FROM range(10)")
