"""Fixes after the pool's test half (2026-10-18): a view's row count for the
GROUP BY rule (P32), subquery filters judged on their own (P51), and NOT IN
over a column with NULLs (P37, T23)."""
from datetime import datetime, timezone
from types import SimpleNamespace

from query_preflight import sql_ast
from query_preflight.backends.bigquery_backend import BigQueryWarehouse
from query_preflight.column_registry import ColumnRegistry, RegistryEntry
from query_preflight.guard import decide
from query_preflight.tracing import Tracer

NOW = datetime.now(timezone.utc).isoformat()
NOT_IN = ("SELECT COUNT(*) AS n FROM users WHERE id NOT IN "
          "(SELECT user_id FROM {t} WHERE created_at >= TIMESTAMP '2026-10-01')")


class Client:
    """users is a view (no row count from the API); events and orders are
    tables partitioned on created_at."""

    def query(self, sql, job_config=None):
        return SimpleNamespace(total_bytes_processed=2 * 1024**2)

    def get_table(self, ref):
        name = ref.rsplit(".", 1)[-1]
        view = name == "users"
        cols = {"users": ("id", "street_address", "country"),
                "events": ("user_id", "created_at"), "orders": ("user_id", "created_at")}[name]
        return SimpleNamespace(
            table_type="VIEW" if view else "TABLE", num_rows=0 if view else 2_000_000,
            num_bytes=0 if view else 50_000_000,
            schema=[SimpleNamespace(name=c) for c in cols],
            time_partitioning=None if view else SimpleNamespace(field="created_at"),
            range_partitioning=None, clustering_fields=None)


def _wh(tmp_path, entries):
    wh = BigQueryWarehouse("p", "ds", client=Client(), registry_path=str(tmp_path / "none"))
    wh._registry, wh._registry_loaded = ColumnRegistry(entries, "ds"), True
    return wh


def _e(table, col, rows, ndv, nulls=0):
    return RegistryEntry("ds", table, col, "INTEGER", rows, ndv, nulls, True, NOW)


USERS = [_e("users", "id", 100_000, 100_000), _e("users", "street_address", 100_000, 100_000),
         _e("users", "country", 100_000, 15)]


def test_group_by_unique_column_of_a_view_warns(tmp_path):
    """P32: a view reports no row count; the registry's 100,000 rows make it large."""
    wh = _wh(tmp_path, USERS)
    v = decide("SELECT street_address, COUNT(*) AS n FROM users GROUP BY street_address",
               backend=wh, tracer=Tracer())
    assert v.decision == "warn" and "near-unique column 'street_address'" in v.reason
    v = decide("SELECT country, COUNT(*) AS n FROM users GROUP BY country", backend=wh, tracer=Tracer())
    assert v.decision == "allow"


def test_a_subquery_partition_filter_is_not_blamed_on_the_outer_comparison():
    """P51: created_at is compared bare inside the subquery."""
    tree = sql_ast.parse_one(NOT_IN.format(t="orders"))
    use = sql_ast.partition_filter_use(tree, "orders", "created_at")
    assert use.wrapped == [] and len(use.bare) == 1
    # a wrapper inside the subquery's own comparison is still caught
    tree = sql_ast.parse_one("SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders "
                             "WHERE EXTRACT(MONTH FROM created_at) = 6)")
    use = sql_ast.partition_filter_use(tree, "orders", "created_at")
    assert len(use.wrapped) == 1 and "EXTRACT" in use.wrapped[0]


def test_not_in_subqueries():
    found = sql_ast.not_in_subqueries(sql_ast.parse_one(NOT_IN.format(t="events")))
    assert [(t, c) for t, c, _ in found] == [("events", "user_id")]
    for sql in (
        # the subquery excludes NULLs itself
        "SELECT 1 FROM users WHERE id NOT IN (SELECT user_id FROM events WHERE user_id IS NOT NULL)",
        # NOT EXISTS is NULL-safe; IN without NOT is not affected
        "SELECT 1 FROM users u WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.user_id = u.id)",
        "SELECT 1 FROM users WHERE id IN (SELECT user_id FROM events)",
        # a literal list, a join in the subquery, a CTE: not judged
        "SELECT 1 FROM users WHERE id NOT IN (1, 2, 3)",
        "SELECT 1 FROM users WHERE id NOT IN (SELECT e.user_id FROM events e JOIN orders o "
        "ON o.user_id = e.user_id)",
        "WITH x AS (SELECT user_id FROM events) SELECT 1 FROM users WHERE id NOT IN (SELECT user_id FROM x)",
    ):
        assert sql_ast.not_in_subqueries(sql_ast.parse_one(sql)) == [], sql
    # `NOT (x IN (...))` is the same predicate
    sql = "SELECT 1 FROM users WHERE NOT (id IN (SELECT user_id FROM events))"
    assert [c for _, c, _ in sql_ast.not_in_subqueries(sql_ast.parse_one(sql))] == ["user_id"]


def test_not_in_over_a_column_with_nulls_warns(tmp_path):
    """P37 / T23: events.user_id has NULLs; orders.user_id has none (P51)."""
    wh = _wh(tmp_path, USERS + [_e("events", "user_id", 2_000_000, 80_000, nulls=900_000),
                                _e("orders", "user_id", 2_000_000, 80_000, nulls=0)])
    v = decide(NOT_IN.format(t="events"), backend=wh, tracer=Tracer())
    assert v.decision == "warn" and "NOT IN over events.user_id, which has 900,000 NULLs" in v.reason
    assert not any("wrapped" in f for f in v.findings)
    v = decide(NOT_IN.format(t="orders"), backend=wh, tracer=Tracer())
    assert v.decision == "allow", v.findings


def test_not_in_without_a_registry_entry_is_a_visible_skip(tmp_path):
    wh = _wh(tmp_path, USERS)
    v = decide(NOT_IN.format(t="events"), backend=wh, tracer=Tracer())
    assert v.decision == "allow"
    assert any(s.startswith("no_not_in_over_nulls on 'events.user_id': column not in the registry")
               for s in v.skipped_rules)
