"""no_redundant_distinct: DISTINCT that provably cannot change the result."""
import pytest

from cost_guard import sql_ast
from cost_guard.guard import decide
from cost_guard.tracing import Tracer


def _found(sql):
    return sql_ast.redundant_distincts(sql_ast.parse_one(sql))


@pytest.mark.parametrize("sql", [
    "SELECT DISTINCT status, COUNT(*) FROM orders GROUP BY status",
    "SELECT DISTINCT status AS s, COUNT(*) FROM orders GROUP BY s",
    "SELECT DISTINCT status, user_id % 2, COUNT(*) FROM orders GROUP BY 1, 2",
    "SELECT DISTINCT o.status, COUNT(*) FROM orders o GROUP BY o.status",
    "SELECT DISTINCT COUNT(*), SUM(amount_cents) FROM orders",
    "SELECT id FROM users WHERE id IN (SELECT DISTINCT user_id FROM orders)",
    "SELECT id FROM users u WHERE EXISTS (SELECT DISTINCT 1 FROM orders o WHERE o.user_id = u.id)",
    "SELECT DISTINCT status FROM orders UNION SELECT DISTINCT country FROM users",
    # T21: the derived table is already unique on its GROUP BY keys.
    "SELECT DISTINCT user_id FROM (SELECT user_id FROM orders WHERE created_at >= '2024-01-01' GROUP BY user_id)",
    "SELECT DISTINCT t.s, t.n FROM (SELECT status AS s, COUNT(*) AS n FROM orders GROUP BY status) t WHERE t.n > 1",
    "SELECT DISTINCT u FROM (SELECT user_id AS u, COUNT(*) FROM orders GROUP BY 1)",
])
def test_redundant(sql):
    assert _found(sql)


@pytest.mark.parametrize("sql", [
    "SELECT DISTINCT status FROM orders",  # DISTINCT does the work
    "SELECT DISTINCT status, COUNT(*) FROM orders GROUP BY status, user_id",  # user_id not selected
    "SELECT DISTINCT ON (status) status, order_id FROM orders",
    "SELECT DISTINCT status, SUM(amount_cents) FROM orders GROUP BY ROLLUP (status)",
    "SELECT DISTINCT status FROM orders UNION ALL SELECT DISTINCT country FROM users",
    "SELECT DISTINCT status, COUNT(*) OVER () FROM orders",
    "SELECT DISTINCT status, MAX(amount_cents) - MIN(amount_cents), user_id FROM orders GROUP BY status, user_id, order_id",
    # Derived table grouped by more than is selected, joined, or not grouped: DISTINCT can matter.
    "SELECT DISTINCT status FROM (SELECT status, user_id FROM orders GROUP BY status, user_id)",
    "SELECT DISTINCT t.user_id FROM (SELECT user_id FROM orders GROUP BY user_id) t JOIN users u ON u.id = t.user_id",
    "SELECT DISTINCT user_id FROM (SELECT user_id FROM orders)",
    "SELECT DISTINCT user_id % 10 FROM (SELECT user_id FROM orders GROUP BY user_id)",
])
def test_not_redundant(sql):
    assert _found(sql) == []


def test_redundant_distinct_preserves_results(synthetic_wh):
    """The rule's claim, checked on data: dropping DISTINCT changes nothing."""
    con = synthetic_wh._connect()
    for sql in ("SELECT DISTINCT status, COUNT(*) FROM orders GROUP BY status",
                "SELECT DISTINCT COUNT(*), SUM(amount_cents) FROM orders"):
        a = sorted(con.execute(sql).fetchall())
        b = sorted(con.execute(sql.replace("DISTINCT ", "", 1)).fetchall())
        assert a == b


def test_guard_warns(synthetic_wh):
    v = decide("SELECT DISTINCT status, COUNT(*) FROM orders WHERE created_at >= '2024-01-01' "
               "GROUP BY status", backend=synthetic_wh, tracer=Tracer())
    assert v.decision == "warn"
    assert any("SELECT DISTINCT is redundant" in f for f in v.findings)
