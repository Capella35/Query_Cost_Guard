"""Join output estimate from the SQL and the column registry (join_estimate.py),
and the Guard's fan-out block on BigQuery, where a dry run has no row counts."""
from datetime import datetime, timedelta, timezone

import pytest

from query_preflight import sql_ast
from query_preflight.column_registry import ColumnRegistry, RegistryEntry
from query_preflight.join_estimate import join_estimates

NOW = datetime.now(timezone.utc).isoformat()
TABLES = {  # table: (rows, {column: ndv})
    "events": (2_400_000, {"user_id": 80_000, "session_id": 680_000, "id": 2_400_000,
                           "created_at": 2_000_000}),
    "users": (100_000, {"id": 100_000, "country": 15}),
    "orders": (125_000, {"order_id": 125_000, "user_id": 80_000}),
}


def _registry(profiled_at=NOW, schema="ds"):
    return ColumnRegistry([
        RegistryEntry(schema, t, c, "INTEGER", rows, ndv, 0, True, profiled_at)
        for t, (rows, cols) in TABLES.items() for c, ndv in cols.items()
    ], default_schema=schema)


def _est(sql, reg=None):
    return join_estimates(sql_ast.parse_one(sql), reg or _registry(), 35)


def test_self_join_fans_out():
    [e] = _est("SELECT a.user_id, COUNT(*) FROM events a JOIN events b ON a.user_id = b.user_id "
               "GROUP BY a.user_id")
    assert e.rows == 2_400_000 * 2_400_000 // 80_000 and e.fanout == pytest.approx(30.0)
    assert not e.filtered and e.keys == ["a.user_id = b.user_id"]


def test_lookup_join_does_not_fan_out():
    [e] = _est("SELECT o.order_id, u.country FROM orders o JOIN users u ON o.user_id = u.id")
    assert e.rows == 125_000 and e.fanout == pytest.approx(1.0)


def test_where_equality_and_using_are_join_keys():
    [w] = _est("SELECT 1 FROM orders o, users u WHERE o.user_id = u.id")
    assert w.rows == 125_000 and not w.filtered
    [u] = _est("SELECT 1 FROM events a JOIN events b USING (session_id)")
    assert u.rows == 2_400_000 * 2_400_000 // 680_000


def test_non_key_predicates_mark_the_estimate_filtered():
    for q in ("SELECT 1 FROM events a JOIN events b ON a.user_id = b.user_id "
              "WHERE a.created_at >= TIMESTAMP '2024-01-01'",
              # an equality on an expression is not a key (Q28's shape)
              "SELECT 1 FROM events a JOIN events b ON a.session_id = b.session_id "
              "AND b.id = a.id + 1"):
        [e] = _est(q)
        assert e.filtered


def test_left_join_keeps_at_least_the_preserved_side():
    reg = ColumnRegistry([RegistryEntry("ds", "a", "k", "INTEGER", 1000, 1000, 0, True, NOW),
                          RegistryEntry("ds", "b", "k", "INTEGER", 10, 10, 0, True, NOW)], "ds")
    [inner] = _est("SELECT 1 FROM a JOIN b ON a.k = b.k", reg)
    [left] = _est("SELECT 1 FROM a LEFT JOIN b ON a.k = b.k", reg)
    assert inner.rows == 10 and left.rows == 1000


@pytest.mark.parametrize("sql", [
    # a subquery or CTE input: its rows are unknown
    "SELECT 1 FROM (SELECT user_id FROM events) a JOIN users u ON a.user_id = u.id",
    "WITH a AS (SELECT user_id FROM events) SELECT 1 FROM a JOIN users u ON a.user_id = u.id",
    # unqualified column: can't tell which relation it belongs to
    "SELECT 1 FROM events a JOIN users u ON user_id = u.id",
    # a table or column the registry doesn't have
    "SELECT 1 FROM events a JOIN sessions s ON a.session_id = s.id",
    "SELECT 1 FROM events a JOIN users u ON a.event_type = u.id",
    # no link at all: the cartesian rule's case
    "SELECT 1 FROM events a CROSS JOIN users u",
])
def test_unknown_inputs_give_no_estimate(sql):
    assert _est(sql) == []


def test_stale_registry_gives_no_estimate():
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    assert _est("SELECT 1 FROM events a JOIN events b ON a.user_id = b.user_id",
                _registry(profiled_at=old)) == []


# ---- Guard on BigQuery --------------------------------------------------------

def _bq_warehouse(tmp_path, pruned_share=0.01):
    pytest.importorskip("google.cloud.bigquery")
    from types import SimpleNamespace

    from query_preflight.backends.bigquery_backend import BigQueryWarehouse

    class Client:
        def query(self, sql, job_config=None):
            # a filter on the partition column reads `pruned_share` of the partitions
            share = pruned_share if "WHERE" in sql and "created_at" in sql.split("WHERE")[-1] else 1
            return SimpleNamespace(total_bytes_processed=int(10 * 1024**2 * share))

        def get_table(self, ref):
            name = ref.rsplit(".", 1)[-1]
            rows, cols = TABLES[name]
            return SimpleNamespace(
                table_type="TABLE", num_rows=rows, num_bytes=0,
                schema=[SimpleNamespace(name=c) for c in cols],
                time_partitioning=SimpleNamespace(field="created_at") if name == "events" else None,
                range_partitioning=None, clustering_fields=None)

    wh = BigQueryWarehouse("p", "ds", client=Client(), registry_path=str(tmp_path / "none"))
    wh._registry, wh._registry_loaded = _registry(), True
    return wh


def test_guard_blocks_unfiltered_fanout_on_bigquery(tmp_path):
    from query_preflight.guard import decide
    from query_preflight.tracing import Tracer

    v = decide("SELECT a.user_id, COUNT(*) AS pairs FROM events a JOIN events b "
               "ON a.user_id = b.user_id GROUP BY a.user_id",
               backend=_bq_warehouse(tmp_path), tracer=Tracer())
    assert v.decision == "block" and "join fan-out" in v.reason and "72,000,000" in v.reason


def test_guard_does_not_act_on_filtered_or_lookup_joins(tmp_path):
    from query_preflight.guard import decide
    from query_preflight.tracing import Tracer

    wh = _bq_warehouse(tmp_path)
    filtered = decide("SELECT a.user_id FROM events a JOIN events b ON a.user_id = b.user_id "
                      "WHERE a.created_at >= TIMESTAMP '2024-01-01' "
                      "AND b.created_at >= TIMESTAMP '2024-01-01'", backend=wh, tracer=Tracer())
    lookup = decide("SELECT o.order_id, u.country FROM orders o JOIN users u ON o.user_id = u.id",
                    backend=wh, tracer=Tracer())
    assert filtered.decision != "block" and lookup.decision != "block"


# ---- GROUP BY columns keep their table (bug found by the BigQuery eval, B18/B19) --

def test_group_by_columns_per_table():
    tree = sql_ast.parse_one("SELECT u.id, COUNT(*) FROM users u JOIN events e "
                             "ON e.state = u.state GROUP BY u.id, status")
    assert sql_ast.group_by_columns(tree) == ["id", "status"]  # all tables: unchanged
    assert sql_ast.group_by_columns(tree, "users") == ["id"]  # unqualified status: unresolved
    assert sql_ast.group_by_columns(tree, "events") == []  # u.id is not events.id
    only_events = lambda t, c: t == "events" and c == "status"  # noqa: E731
    assert sql_ast.group_by_columns(tree, "events", has_column=only_events) == ["status"]
    assert sql_ast.group_by_columns(tree, "users", has_column=only_events) == ["id"]
    # through a derived table or CTE whose output is a plain column
    sub = sql_ast.parse_one("SELECT t.id FROM (SELECT id FROM events) t GROUP BY t.id")
    assert sql_ast.group_by_columns(sub, "events") == ["id"]
    cte = sql_ast.parse_one("WITH r AS (SELECT user_id AS u FROM orders) SELECT u, COUNT(*) FROM r GROUP BY u")
    assert sql_ast.group_by_columns(cte, "orders") == ["u"] and sql_ast.group_by_columns(cte, "events") == []


def test_group_by_column_is_judged_in_its_own_scope():
    """H28's shape: the CTE groups orders.user_id; the outer query reads
    events. user_id belongs to orders only, though events has a user_id too."""
    tree = sql_ast.parse_one(
        "WITH r AS (SELECT user_id, COUNT(*) AS n FROM orders GROUP BY user_id) "
        "SELECT e.event_type, AVG(r.n) FROM r JOIN events e ON e.user_id = r.user_id GROUP BY e.event_type")
    has = lambda t, c: c in {"orders": {"user_id"}, "events": {"user_id", "event_type"}}[t]  # noqa: E731
    assert sql_ast.group_by_columns(tree, "orders", has_column=has) == ["user_id"]
    assert sql_ast.group_by_columns(tree, "events", has_column=has) == ["event_type"]


def test_group_by_qualified_column_is_judged_on_its_own_table(tmp_path):
    from query_preflight.guard import decide
    from query_preflight.tracing import Tracer

    v = decide("SELECT u.id, COUNT(*) AS n FROM users u JOIN events e ON e.user_id = u.id "
               "WHERE e.created_at >= TIMESTAMP '2023-12-01' GROUP BY u.id",
               backend=_bq_warehouse(tmp_path), tracer=Tracer())
    near_unique = [f for f in v.findings if "near-unique" in f]
    assert near_unique and all("in 'users'" in f for f in near_unique), v.findings


def test_partition_filter_scales_the_join_by_the_measured_fraction(tmp_path):
    """A date filter on the partition column is applied as the share of
    partitions a dry run reads (B19's shape): 30% of events still fans out
    past the cap, 1% does not."""
    from query_preflight.guard import decide
    from query_preflight.tracing import Tracer

    q = ("SELECT a.user_id FROM events a JOIN events b ON a.user_id = b.user_id "
         "WHERE a.created_at >= TIMESTAMP '2023-12-01' AND b.created_at >= TIMESTAMP '2023-12-01'")
    wide = decide(q, backend=_bq_warehouse(tmp_path, pruned_share=0.3), tracer=Tracer())
    narrow = decide(q, backend=_bq_warehouse(tmp_path, pruned_share=0.01), tracer=Tracer())
    assert wide.decision == "block" and "join fan-out" in wide.reason
    assert narrow.decision != "block"


def test_other_filters_still_keep_the_estimate_out_of_decisions(tmp_path):
    from query_preflight.guard import decide
    from query_preflight.tracing import Tracer

    v = decide("SELECT a.user_id FROM events a JOIN events b ON a.user_id = b.user_id "
               "WHERE a.event_type = 'purchase'",
               backend=_bq_warehouse(tmp_path, pruned_share=1), tracer=Tracer())
    assert "join fan-out" not in v.reason


def test_partition_fraction_hook_unit():
    seen = []

    def frac(table, column, conds):
        seen.append((table, column, [c.sql() for c in conds]))
        return 0.5

    [e] = join_estimates(sql_ast.parse_one(
        "SELECT 1 FROM events a JOIN users u ON a.user_id = u.id "
        "WHERE a.created_at >= '2023-01-01'"), _registry(), 35,
        partition_columns={"events": "created_at"}, partition_fraction=frac)
    assert seen == [("events", "created_at", ["created_at >= '2023-01-01'"])]
    assert not e.filtered and e.rows == 1_200_000 * 100_000 // 100_000


def test_cartesian_reads_a_view_row_count_from_the_registry(tmp_path):
    """T27: BigQuery reports no row count for a view, so a 10-row view never
    counted as tiny and a cross join with it was blocked. The registry
    profiled the view; its row count decides."""
    from types import SimpleNamespace

    from query_preflight.backends.bigquery_backend import BigQueryWarehouse
    from query_preflight.guard import decide
    from query_preflight.tracing import Tracer

    class Client:
        def query(self, sql, job_config=None):
            return SimpleNamespace(total_bytes_processed=1024**2)

        def get_table(self, ref):
            name = ref.rsplit(".", 1)[-1]
            view = name == "centers"
            return SimpleNamespace(
                table_type="VIEW" if view else "TABLE", num_rows=0 if view else 100_000, num_bytes=0,
                schema=[SimpleNamespace(name=c) for c in ("id", "name", "country")],
                time_partitioning=None, range_partitioning=None, clustering_fields=None)

    q = "SELECT u.id, c.name FROM users u CROSS JOIN centers c WHERE u.country = 'Japan'"
    for centers_rows, expected_block in ((10, False), (50_000, True)):
        wh = BigQueryWarehouse("p", "ds", client=Client(), registry_path=str(tmp_path / "none"))
        wh._registry = ColumnRegistry(
            [RegistryEntry("ds", "users", "id", "INTEGER", 100_000, 100_000, 0, True, NOW),
             RegistryEntry("ds", "centers", "id", "INTEGER", centers_rows, centers_rows, 0, True, NOW)], "ds")
        wh._registry_loaded = True
        v = decide(q, backend=wh, tracer=Tracer())
        assert (v.decision == "block") == expected_block, (centers_rows, v.decision, v.findings)
    wh = BigQueryWarehouse("p", "ds", client=Client(), registry_path=str(tmp_path / "none"))
    wh._registry, wh._registry_loaded = None, True  # no registry: not provably tiny
    assert decide(q, backend=wh, tracer=Tracer()).decision == "block"


def _users_registry():
    return ColumnRegistry([
        RegistryEntry("ds", "users", c, "STRING", 100_000, ndv, 0, True, NOW)
        for c, ndv in (("id", 100_000), ("gender", 2), ("country", 16))
    ], default_schema="ds")


def test_value_filters_are_applied_as_a_share_of_rows():
    """T08: an equality or IN filter on a profiled column keeps 1/ndv (n/ndv)
    of the rows, so the estimate is no longer 'filtered' (traced only)."""
    reg = _users_registry()
    (e,) = _est("SELECT a.id, b.id FROM users a JOIN users b ON a.gender = b.gender "
                "WHERE a.country = 'Japan'", reg)
    assert not e.filtered and e.rows == 100_000 // 16 * 100_000 // 2  # 312.5M
    (e,) = _est("SELECT a.id, b.id FROM users a JOIN users b ON a.gender = b.gender "
                "WHERE a.country IN ('Japan', 'Spain') AND b.country = 'Japan'", reg)
    assert not e.filtered and e.rows == 12_500 * 6_250 // 2
    (e,) = _est("SELECT a.id FROM users a JOIN users b ON a.gender = b.gender "
                "WHERE a.country LIKE 'J%'", reg)  # not a value filter: still traced only
    assert e.filtered


def test_value_counts_replace_one_over_ndv_for_listed_values():
    """P11: Colombia is 21 of 100,000 users; 1/16 by ndv would be 6,250."""
    reg = _users_registry()
    reg.add_value_counts([("ds", "users", "country", "Colombia", 21),
                          ("ds", "users", "country", "China", 34_000)])
    (e,) = _est("SELECT a.id, b.id FROM users a JOIN users b ON a.gender = b.gender "
                "WHERE a.country = 'Colombia'", reg)
    assert not e.filtered and e.rows == 21 * 100_000 // 2  # 1.05M, under the 5M cap
    # IN mixes stored and unstored values: Japan has no count, so 1/16
    (e,) = _est("SELECT a.id, b.id FROM users a JOIN users b ON a.gender = b.gender "
                "WHERE a.country IN ('Colombia', 'China', 'Japan')", reg)
    assert e.rows == (21 + 34_000 + 6_250) * 100_000 // 2
    # a stale entry: the count is not trusted either
    stale = ColumnRegistry([RegistryEntry("ds", "users", c, "STRING", 100_000, ndv, 0, True,
                                          "2020-01-01T00:00:00+00:00")
                            for c, ndv in (("id", 100_000), ("gender", 2), ("country", 16))], "ds")
    stale.add_value_counts([("ds", "users", "country", "Colombia", 21)])
    assert _est("SELECT a.id FROM users a JOIN users b ON a.gender = b.gender "
                "WHERE a.country = 'Colombia'", stale) == []


def test_guard_blocks_filtered_fanout_with_a_value_filter(tmp_path):
    from types import SimpleNamespace

    from query_preflight.backends.bigquery_backend import BigQueryWarehouse
    from query_preflight.guard import decide
    from query_preflight.tracing import Tracer

    class Client:
        def query(self, sql, job_config=None):
            return SimpleNamespace(total_bytes_processed=2 * 1024**2)

        def get_table(self, ref):
            return SimpleNamespace(
                table_type="TABLE", num_rows=100_000, num_bytes=0,
                schema=[SimpleNamespace(name=c) for c in ("id", "gender", "country")],
                time_partitioning=None, range_partitioning=None, clustering_fields=None)

    wh = BigQueryWarehouse("p", "ds", client=Client(), registry_path=str(tmp_path / "none"))
    wh._registry, wh._registry_loaded = _users_registry(), True
    v = decide("SELECT a.id, b.id AS same_gender_user FROM users a JOIN users b "
               "ON a.gender = b.gender WHERE a.country = 'Japan'", backend=wh, tracer=Tracer())
    assert v.decision == "block" and "join fan-out" in v.reason


def _age_registry(complete=True):
    """users.age: 4 values; complete when the counts add up to the rows."""
    reg = ColumnRegistry([
        RegistryEntry("ds", "users", "id", "INTEGER", 100_000, 100_000, 0, True, NOW),
        RegistryEntry("ds", "users", "gender", "STRING", 100_000, 2, 0, True, NOW),
        RegistryEntry("ds", "users", "age", "INTEGER", 100_000, 4, 0, True, NOW),
    ], default_schema="ds")
    counts = {"29": 40_000, "30": 3_000, "31": 2_000, "32": 55_000}
    if not complete:
        counts.pop("32")  # the most common values only: 45,000 of 100,000 rows
    reg.add_value_counts([("ds", "users", "age", v, n) for v, n in counts.items()])
    return reg


def test_range_filter_uses_complete_value_counts():
    """P09: age BETWEEN 30 AND 31 keeps exactly 5,000 of 100,000 users."""
    reg = _age_registry()
    q = ("SELECT a.id, b.id FROM users a JOIN users b ON a.gender = b.gender "
         "WHERE a.age {}")
    for cond in ("BETWEEN 30 AND 31", ">= 30 AND a.age <= 31", "> 29 AND a.age < 32"):
        (e,) = _est(q.format(cond), reg)
        assert not e.filtered and e.rows == 5_000 * 100_000 // 2, cond  # combined, not multiplied
    (e,) = _est(q.format(">= 32"), reg)
    assert not e.filtered and e.rows == 55_000 * 100_000 // 2
    (e,) = _est("SELECT a.id FROM users a JOIN users b ON a.gender = b.gender WHERE 31 >= a.age", reg)
    assert e.rows == 45_000 * 100_000 // 2  # literal on the left


def test_range_filter_without_complete_counts_stays_traced():
    (e,) = _est("SELECT a.id, b.id FROM users a JOIN users b ON a.gender = b.gender "
                "WHERE a.age BETWEEN 30 AND 31", _age_registry(complete=False))
    assert e.filtered
    # a string column, or a column without counts: traced only
    (e,) = _est("SELECT a.id FROM users a JOIN users b ON a.gender = b.gender "
                "WHERE a.gender > 'F'", _age_registry())
    assert e.filtered


def _source_registry():
    reg = ColumnRegistry([
        RegistryEntry("ds", "users", c, "STRING", 100_000, ndv, 0, True, NOW)
        for c, ndv in (("id", 99_651), ("traffic_source", 5), ("country", 15), ("city", 7_915))
    ], default_schema="ds")
    reg.add_value_counts([("ds", "users", "country", "Spain", 3_992)])
    return reg


def test_inequality_on_a_unique_column_keeps_the_estimate():
    """Agent eval A04: 'every OTHER user of the same traffic source'. a.id != b.id
    removes each Spanish user's pair with itself: at most 3,992 rows."""
    (e,) = _est("SELECT a.id, b.id FROM users a JOIN users b ON a.traffic_source = b.traffic_source "
                "WHERE a.country = 'Spain' AND a.id != b.id", _source_registry())
    assert not e.filtered and e.rows == 3_992 * 100_000 // 5 - 3_992


def test_other_inequalities_stay_traced():
    reg = _source_registry()
    # not unique on either side: how many pairs it removes is unknown
    (e,) = _est("SELECT a.id FROM users a JOIN users b ON a.traffic_source = b.traffic_source "
                "WHERE a.city <> b.city", reg)
    assert e.filtered
    # the join key itself: contradicts the equality, the estimate means nothing
    (e,) = _est("SELECT a.id FROM users a JOIN users b ON a.id = b.id WHERE a.id <> b.id", reg)
    assert e.filtered
