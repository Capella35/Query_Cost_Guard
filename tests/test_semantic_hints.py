"""Semantic hints (draft, 2026-10-24): the TheLook file loads and validates;
the two rules flag the measured mistakes and leave the right forms alone."""
from pathlib import Path

import pytest

from query_preflight.semantic_hints import check, load_hints

HINTS = load_hints(Path(__file__).resolve().parent.parent / "semantic_hints.yaml")


def test_the_thelook_file_loads_with_its_keys_and_joins():
    assert HINTS.tables["orders"]["key"] == "order_id" and len(HINTS.joins) == 9
    assert (("order_items", "order_id"), ("orders", "order_id")) in HINTS.joins
    assert "never multiply" in HINTS.columns[("orders", "num_of_item")]["note"]


def test_a_join_whose_one_side_is_not_a_key_is_refused(tmp_path):
    f = tmp_path / "h.yaml"
    f.write_text("tables: {orders: {key: order_id}}\njoins: [order_items.user_id = orders.user_id]\n")
    with pytest.raises(ValueError, match="not orders's declared key"):
        load_hints(f)


# ---- join keys --------------------------------------------------------------

def test_join_on_an_undeclared_key_is_flagged():  # owner test O03's mistake
    f = check("SELECT o.gender, SUM(oi.sale_price) FROM order_items oi "
              "JOIN orders o ON oi.user_id = o.user_id GROUP BY o.gender", HINTS)
    assert len(f) == 1 and f[0].startswith("join key not declared")
    assert "order_items.order_id = orders.order_id" in f[0]


@pytest.mark.parametrize("on", ["oi.order_id = o.order_id",
                                "oi.order_id = o.order_id AND oi.user_id = o.user_id"])
def test_the_declared_key_is_fine_even_with_extra_equalities(on):
    assert check(f"SELECT COUNT(*) FROM order_items oi JOIN orders o ON {on}", HINTS) == []


def test_a_comma_join_in_where_is_checked_too():
    f = check("SELECT COUNT(*) FROM order_items oi, orders o WHERE oi.user_id = o.user_id", HINTS)
    assert f and f[0].startswith("join key not declared")


def test_tables_without_a_declared_join_are_left_alone():
    assert check("SELECT COUNT(*) FROM events e JOIN orders o ON e.user_id = o.user_id", HINTS) == []


# ---- fan-out sums -------------------------------------------------------------

def test_the_num_of_item_double_count_is_flagged():  # owner test O05 / O06's mistake
    f = check("SELECT SUM(oi.sale_price * o.num_of_item) AS revenue FROM order_items oi "
              "JOIN orders o ON oi.order_id = o.order_id", HINTS)
    assert len(f) == 1 and f[0].startswith("fan-out SUM")
    assert "total is multiplied" in f[0] and "never multiply" in f[0]


def test_avg_is_called_weighted():
    f = check("SELECT AVG(u.age) FROM order_items oi JOIN users u ON oi.user_id = u.id", HINTS)
    assert f and "average is weighted by order_items rows" in f[0]


@pytest.mark.parametrize("sql", [
    # the many side's own column: one value per row, nothing repeats
    "SELECT SUM(oi.sale_price) FROM order_items oi JOIN orders o ON oi.order_id = o.order_id",
    # counting is not summing
    "SELECT COUNT(DISTINCT o.order_id), MAX(o.num_of_item) FROM order_items oi "
    "JOIN orders o ON oi.order_id = o.order_id",
    # the many side pre-aggregated: its own SELECT, one row per order
    "SELECT SUM(o.num_of_item) FROM orders o JOIN (SELECT order_id, COUNT(*) AS n "
    "FROM order_items GROUP BY order_id) x ON x.order_id = o.order_id",
    # one table: nothing fans out
    "SELECT SUM(num_of_item) FROM orders",
])
def test_the_right_forms_are_not_flagged(sql):
    assert check(sql, HINTS) == []


def test_unqualified_columns_resolve_with_the_registry_schema():
    cols = {"orders": {"order_id", "user_id", "num_of_item"},
            "order_items": {"id", "order_id", "user_id", "sale_price"}}
    has = lambda t, c: c in cols.get(t, set())  # noqa: E731
    sql = ("SELECT SUM(sale_price * num_of_item) FROM order_items oi "
           "JOIN orders o ON oi.order_id = o.order_id")
    assert check(sql, HINTS) == []  # unsure without a schema: say nothing
    assert check(sql, HINTS, has)[0].startswith("fan-out SUM")


def test_the_eval_scores_catches_misses_and_false_alarms():
    """The scorer, on two throwaway queries (never the pre-registered set)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
    from run_hints_eval import score

    queries = [
        {"id": "a", "expect": "join_key", "sql": "SELECT COUNT(*) FROM order_items oi "
                                                 "JOIN orders o ON oi.user_id = o.user_id"},
        {"id": "b", "expect": "none", "sql": "SELECT SUM(o.num_of_item) FROM orders o "
                                             "JOIN order_items oi ON oi.order_id = o.order_id"},
    ]
    rows, m = score(queries, HINTS, None)
    assert m["join_key_caught"] == "1/1" and m["false_alarms_on_correct"] == "1/1"
    assert rows[1]["got"] == ["fanout"] and m["exact"] == "1/2"
