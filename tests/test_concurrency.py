"""One backend shared by many users at once (a long-running service): checks
must neither crash nor change verdicts. Before per-thread cursors, 8 threads
on a shared DuckDBWarehouse gave 76 errors and 47 wrong verdicts in 200."""
from concurrent.futures import ThreadPoolExecutor

from query_preflight.backends.duckdb_backend import DuckDBWarehouse
from query_preflight.guard import decide
from query_preflight.tracing import Tracer

QUERIES = [
    "SELECT status, COUNT(*) FROM orders WHERE created_at >= '2024-01-01' GROUP BY status",
    "SELECT * FROM users, orders",  # block (cartesian)
    "SELECT SUM(user_id) FROM orders WHERE created_at >= '2024-01-01'",  # block (id aggregate)
    "SELECT order_id FROM orders WHERE DATE_TRUNC('month', created_at) = DATE '2024-02-01'",  # rewrite
    "SELECT u.country, COUNT(*) FROM orders o JOIN users u ON u.id = o.user_id "
    "WHERE o.created_at >= '2024-03-01' GROUP BY 1",
]


def test_shared_backend_gives_the_same_verdicts_under_concurrency(synthetic_wh):
    wh = DuckDBWarehouse(synthetic_wh.db_path)  # fresh instance: nothing warmed up
    single = {q: decide(q, backend=DuckDBWarehouse(synthetic_wh.db_path), tracer=Tracer())
              for q in QUERIES}

    def one(i):
        q = QUERIES[i % len(QUERIES)]
        v = decide(q, backend=wh, tracer=Tracer())
        return q, v.decision, v.estimated_rows

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(one, range(8 * 20)))
    for q, decision, rows in results:
        assert decision == single[q].decision, q
        assert rows == single[q].estimated_rows, q
