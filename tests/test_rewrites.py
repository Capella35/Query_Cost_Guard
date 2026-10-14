"""Tests for the deterministic rewrite fixers."""

from cost_guard.rewrites import parenthesized_subqueries, suggest_cte_rewrite


def test_extracts_nested_subqueries():
    sql = (
        "SELECT * FROM (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) a "
        "JOIN (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) b "
        "USING (user_id)"
    )
    subs = parenthesized_subqueries(sql)
    assert len(subs) == 2
    assert all(s.lower().startswith("(select") for s in subs)


def test_suggest_cte_rewrite():
    sql = (
        "SELECT * FROM (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) a "
        "JOIN (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) b "
        "USING (user_id) ORDER BY user_id LIMIT 50"
    )
    out = suggest_cte_rewrite(sql)
    assert out is not None
    assert out.startswith("WITH cte_1 AS (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id)")
    # both occurrences replaced, aliases preserved
    assert out.count("cte_1") == 3  # WITH def + 2 references
    assert " a JOIN cte_1 b " in out


def test_suggest_cte_rewrite_none_when_no_dupe():
    assert suggest_cte_rewrite("SELECT id, name FROM users WHERE id = 1") is None
