"""The 11 inline Guard cases, written alongside the rules against the
synthetic warehouse (demo/warehouse.py). They are regression tests
(tests/test_guard_regression.py), NOT evidence: 100% here is expected.
The headline eval is the 30-query TheLook set (eval/eval_queries.yaml)."""

CASES = [
    {
        "id": "E01",
        "sql": "SELECT * FROM events WHERE created_at >= '2024-06-01'",
        "expected": "warn",
        "why": "SELECT * on wide table",
    },
    {
        "id": "E02",
        "sql": "SELECT * FROM users, orders",
        "expected": "block",
        "why": "accidental cartesian product",
    },
    {
        "id": "E03",
        "sql": "SELECT * FROM users CROSS JOIN orders",
        "expected": "block",
        "why": "explicit cross join",
    },
    {"id": "E04", "sql": "DELETE FROM users", "expected": "block", "why": "non-read-only"},
    {
        "id": "E05",
        "sql": (
            "SELECT * FROM "
            "(SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) a "
            "JOIN (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) b "
            "USING (user_id) ORDER BY user_id LIMIT 50"
        ),
        "expected": "rewrite",
        "why": "repeated subquery -> auto-suggested CTE (verified)",
    },
    {
        "id": "E06",
        "sql": (
            "SELECT p.category, SUM(oi.quantity * p.retail_price_cents) / 100.0 "
            "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
            "JOIN products p ON p.id = oi.product_id "
            "GROUP BY p.category ORDER BY 1 DESC LIMIT 20"
        ),
        "expected": "warn",
        "why": "revenue query, no date filter",
    },
    {
        "id": "E07",
        "sql": "SELECT id, name FROM users WHERE id = 42",
        "expected": "allow",
        "why": "clean point lookup",
    },
    {
        "id": "E08",
        "sql": "SELECT category, COUNT(*) FROM products GROUP BY category",
        "expected": "allow",
        "why": "clean aggregate",
    },
    {
        "id": "E09",
        "sql": (
            "SELECT order_id, status FROM orders "
            "WHERE created_at >= '2024-01-01' AND status = 'complete' LIMIT 100"
        ),
        "expected": "allow",
        "why": "filtered, projected",
    },
    {
        "id": "E10",
        "sql": (
            "SELECT u.name, COUNT(o.order_id) FROM users u "
            "JOIN orders o ON o.user_id = u.id GROUP BY u.name LIMIT 50"
        ),
        "expected": "warn",
        "why": "join over 1M-row orders, no date filter",
    },
    {
        "id": "E11",
        "sql": "SELECT session_id, COUNT(*) FROM events GROUP BY session_id",
        "expected": "warn",
        "why": "GROUP BY on near-unique column (profile-aware)",
    },
]

