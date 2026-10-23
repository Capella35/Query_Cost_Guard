"""no_sum_on_identifier: SUM/AVG over an identifier column. An id named as
one (id, *_id, *_key, ...) is BLOCKED with a COUNT suggestion; one found only
by near-unique values in the registry (could be a real amount) warns."""
import duckdb
import pytest

from query_preflight.backends.duckdb_backend import DuckDBWarehouse
from query_preflight.column_registry import RegistryEntry, build_registry
from query_preflight.guard import decide
from query_preflight.tracing import Tracer


def _ids(v):
    return [f for f in v.findings if "identifier column" in f]


@pytest.mark.parametrize("sql", [
    "SELECT SUM(user_id) FROM orders WHERE created_at >= '2024-01-01'",
    "SELECT o.status, AVG(o.user_id) FROM orders o WHERE o.created_at >= '2024-01-01' GROUP BY 1",
    "SELECT SUM(CAST(order_id AS DOUBLE)) FROM orders WHERE created_at >= '2024-01-01'",
])
def test_identifier_aggregates_are_blocked_with_a_count(synthetic_wh, sql):
    v = decide(sql, backend=synthetic_wh, tracer=Tracer())
    assert v.decision == "block" and "meaningless result" in v.reason
    assert _ids(v) and "COUNT(DISTINCT" in _ids(v)[0]
    suggested = v.reason.split("Suggested query: ", 1)[1]
    assert "SUM(" not in suggested.upper() and "AVG(" not in suggested.upper()
    synthetic_wh._connect().execute(suggested).fetchall()  # the suggestion runs


def test_window_sum_on_identifier_is_blocked(synthetic_wh):
    v = decide("SELECT status, SUM(user_id) OVER (PARTITION BY status) FROM orders "
               "WHERE created_at >= '2024-01-01'", backend=synthetic_wh, tracer=Tracer())
    assert v.decision == "block" and _ids(v)


def test_only_the_identifier_aggregate_is_replaced(synthetic_wh):
    v = decide("SELECT status, SUM(user_id) AS u, SUM(amount_cents) AS revenue FROM orders "
               "WHERE created_at >= '2024-01-01' GROUP BY status", backend=synthetic_wh, tracer=Tracer())
    suggested = v.reason.split("Suggested query: ", 1)[1]
    assert "COUNT(DISTINCT user_id) AS u" in suggested and "SUM(amount_cents) AS revenue" in suggested


@pytest.mark.parametrize("sql", [
    "SELECT SUM(amount_cents) FROM orders WHERE created_at >= '2024-01-01'",
    "SELECT COUNT(DISTINCT user_id), MAX(order_id) FROM orders WHERE created_at >= '2024-01-01'",
    "SELECT SUM(amount_cents * user_id) FROM orders WHERE created_at >= '2024-01-01'",
    # Qualified by another table's alias: not attributed to orders.
    "SELECT SUM(u.age_band) FROM orders o JOIN users u ON u.id = o.user_id "
    "WHERE o.created_at >= '2024-01-01'",
])
def test_non_identifier_aggregates_pass(synthetic_wh, sql):
    assert _ids(decide(sql, backend=synthetic_wh, tracer=Tracer())) == []


@pytest.fixture()
def ticket_db(tmp_path):
    db = str(tmp_path / "t.duckdb")
    con = duckdb.connect(db)
    con.execute("CREATE TABLE sales AS SELECT range AS ticket, range % 7 AS qty, "
                "range * 1.5 AS price FROM range(5000)")
    con.close()
    return db


def test_near_unique_integer_needs_the_registry(ticket_db, monkeypatch):
    q = "SELECT SUM(ticket), SUM(qty), SUM(price) FROM sales"
    # No registry: name pattern only -> 'ticket' is not caught.
    assert _ids(decide(q, backend=DuckDBWarehouse(ticket_db), tracer=Tracer())) == []
    build_registry(ticket_db)
    found = _ids(decide(q, backend=DuckDBWarehouse(ticket_db), tracer=Tracer()))
    assert len(found) == 1 and "sales.ticket" in found[0] and "near-unique" in found[0]
    # Found by values only (it could be a real amount): a warning, not a block.
    assert decide(q, backend=DuckDBWarehouse(ticket_db), tracer=Tracer()).decision == "warn"
    # A stale registry entry is not trusted: back to name-only.
    monkeypatch.setattr(RegistryEntry, "age_days", lambda self, now=None: 90.0)
    assert _ids(decide(q, backend=DuckDBWarehouse(ticket_db), tracer=Tracer())) == []
