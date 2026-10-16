"""F4.4: BigQuery backend wiring, against a fake client (no GCP project).
Verifies dry-run bytes flow into the Guard's bytes cap; does NOT prove the
backend works against live BigQuery."""
from types import SimpleNamespace

import pytest

pytest.importorskip("google.cloud.bigquery")

from cost_guard.backends.bigquery_backend import BigQueryWarehouse  # noqa: E402
from cost_guard.guard import decide  # noqa: E402
from cost_guard.tracing import Tracer  # noqa: E402


class FakeClient:
    def __init__(self, nbytes, time_partitioning=SimpleNamespace(field="created_at"),
                 range_partitioning=None, columns=("order_id", "user_id", "created_at"),
                 views=()):
        self.nbytes = nbytes
        self.views = views
        self.configs = []
        self.time_partitioning = time_partitioning
        self.range_partitioning = range_partitioning
        self.columns = columns

    def query(self, sql, job_config=None):
        self.configs.append(job_config)
        return SimpleNamespace(total_bytes_processed=self.nbytes, result=lambda timeout=None: [])

    def get_table(self, ref):
        view = ref.rsplit(".", 1)[-1] in self.views
        return SimpleNamespace(
            table_type="VIEW" if view else "TABLE",
            num_rows=0 if view else 125_226, num_bytes=10_000_000,
            schema=[SimpleNamespace(name=n) for n in self.columns],
            time_partitioning=self.time_partitioning, range_partitioning=self.range_partitioning,
            clustering_fields=None,
        )


def test_dry_run_estimate_and_bytes_cap():
    fake = FakeClient(nbytes=500 * 1024**3)  # 500 GB: over the 200 GB interactive cap, under 1 TB scheduled
    wh = BigQueryWarehouse("proj", client=fake)
    est = wh.estimate("SELECT order_id FROM orders")
    assert est.source == "dry_run" and est.estimated_bytes_scanned == 500 * 1024**3
    assert fake.configs[0].dry_run and fake.configs[0].maximum_bytes_billed
    v = decide("SELECT order_id FROM orders WHERE created_at >= '2024-01-01'",
               backend=wh, tracer=Tracer())
    assert v.decision == "block" and "bytes scanned" in v.reason
    sched = decide("SELECT order_id FROM orders WHERE created_at >= '2024-01-01'",
                   "scheduled", backend=wh, tracer=Tracer())
    assert sched.decision != "block"  # 1 TB scheduled cap


def test_metadata_and_watchdog_config():
    fake = FakeClient(nbytes=0)
    wh = BigQueryWarehouse("proj", client=fake)
    md = wh.table_metadata("orders")
    assert md["partitioned_by"] == "created_at" and md["column_count"] == 3
    assert wh.execute("SELECT 1", timeout_sec=2).status == "completed"
    assert int(fake.configs[-1].job_timeout_ms) == 2000


@pytest.mark.parametrize("kw, expected", [
    ({}, "created_at"),
    ({"time_partitioning": SimpleNamespace(field=None)}, "_PARTITIONTIME"),  # ingestion time
    ({"time_partitioning": None, "range_partitioning": SimpleNamespace(field="user_id")}, "user_id"),
    ({"time_partitioning": None}, None),
])
def test_partition_column_read_from_table(kw, expected):
    assert BigQueryWarehouse("proj", client=FakeClient(0, **kw)).table_metadata("t")["partitioned_by"] == expected


def _partition_findings(v):
    return [f for f in v.findings if "full scan" in f]


def test_partition_rule_uses_warehouse_for_table_not_in_customer_yaml():
    wh = BigQueryWarehouse("proj", client=FakeClient(0))
    v = decide("SELECT order_id FROM sessions", backend=wh, tracer=Tracer())  # not in customer.yaml
    assert _partition_findings(v) and v.decision == "warn"
    ok = decide("SELECT order_id FROM sessions WHERE created_at >= '2024-01-01'", backend=wh, tracer=Tracer())
    assert not _partition_findings(ok)


def test_warehouse_partition_column_wins_over_customer_yaml():
    # customer.yaml says orders is partitioned on created_at; the table says order_date.
    fake = FakeClient(0, time_partitioning=SimpleNamespace(field="order_date"),
                      columns=("order_id", "order_date", "created_at"))
    wh = BigQueryWarehouse("proj", client=fake)
    t = Tracer()
    v = decide("SELECT order_id FROM orders WHERE created_at >= '2024-01-01'", backend=wh, tracer=t)
    assert any("order_date" in f for f in _partition_findings(v))  # created_at prunes nothing
    mism = [s for s in t.spans if s["name"] == "partition_column.mismatch"]
    assert mism and mism[0]["attrs"]["warehouse"] == "order_date"
    ok = decide("SELECT order_id FROM orders WHERE order_date >= '2024-01-01'", backend=wh, tracer=Tracer())
    assert not _partition_findings(ok)


def test_ingestion_time_partition_filter():
    wh = BigQueryWarehouse("proj", client=FakeClient(0, time_partitioning=SimpleNamespace(field=None)))
    bare = decide("SELECT order_id FROM sessions", backend=wh, tracer=Tracer())
    assert any("_PARTITIONTIME" in f for f in _partition_findings(bare))
    v = decide("SELECT order_id FROM sessions WHERE _PARTITIONTIME >= TIMESTAMP '2024-01-01'",
               backend=wh, tracer=Tracer())
    assert not _partition_findings(v)


# Warehouse-aware no_function_on_partition_column: forms measured by dry run
# (eval/RESULTS_bq_dryrun_*.md) to prune on BigQuery don't warn there; they
# still warn on DuckDB, which only skips row groups for the bare column.
PRUNED_ON_BQ = [
    "CAST(created_at AS DATE) = DATE '2024-03-01'",
    "CAST(created_at AS DATE) BETWEEN DATE '2024-03-01' AND DATE '2024-03-07'",
    "DATE(created_at) >= DATE '2024-03-01'",
    "TIMESTAMP_TRUNC(created_at, MONTH) = TIMESTAMP '2024-03-01'",
    "DATE_TRUNC(DATE(created_at), MONTH) = DATE '2024-03-01'",
    "FORMAT_TIMESTAMP('%Y-%m-%d', created_at) = '2024-03-01'",
    "TIMESTAMP_SUB(created_at, INTERVAL 1 DAY) >= TIMESTAMP '2024-03-01'",
    "DATE_ADD(DATE(created_at), INTERVAL 1 DAY) = DATE '2024-03-02'",
]
NOT_PRUNED_ON_BQ = [
    "EXTRACT(DAY FROM created_at) = 24",
    "UNIX_SECONDS(created_at) >= 1700000000",
    "created_at + INTERVAL 1 DAY >= TIMESTAMP '2024-03-02'",
    "FORMAT_TIMESTAMP('%Y-%m', created_at) = '2024-03'",
    "FORMAT_DATE('%Y-%m-%d', DATE(created_at)) = '2024-03-01'",
]


def _wrap_findings(v):
    return [f for f in v.findings if "is wrapped in an expression" in f]


@pytest.mark.parametrize("where", PRUNED_ON_BQ)
def test_bigquery_prunable_wrapper_does_not_warn(where):
    wh = BigQueryWarehouse("proj", client=FakeClient(0))
    v = decide(f"SELECT order_id FROM orders WHERE {where}", backend=wh, tracer=Tracer())
    assert not _wrap_findings(v)
    assert not _partition_findings(v)  # still counts as a partition filter


@pytest.mark.parametrize("where", NOT_PRUNED_ON_BQ)
def test_bigquery_unprunable_wrapper_warns(where):
    wh = BigQueryWarehouse("proj", client=FakeClient(0))
    v = decide(f"SELECT order_id FROM orders WHERE {where}", backend=wh, tracer=Tracer())
    assert _wrap_findings(v) and v.decision == "warn"


def test_duckdb_still_warns_on_cast(synthetic_wh):
    v = decide("SELECT order_id FROM orders WHERE CAST(created_at AS DATE) = DATE '2024-03-01'",
               backend=synthetic_wh, tracer=Tracer())
    assert _wrap_findings(v)


def test_view_row_count_is_unknown_not_tiny():
    """BigQuery reports num_rows 0 for a view. Taken as a fact, it made the
    views in a cartesian product "provably tiny" and the rule stayed quiet
    (found live: Q06/Q12 on the TheLook copies, where users is a view)."""
    wh = BigQueryWarehouse("proj", client=FakeClient(0, views=("users", "products")))
    assert wh.table_metadata("users")["num_rows"] is None
    assert wh.table_metadata("orders")["num_rows"] == 125_226
    for q in ("SELECT u.id, p.id FROM users AS u CROSS JOIN products AS p",
              "SELECT o.order_id, u.email FROM orders AS o, users AS u WHERE o.status = 'Shipped'"):
        v = decide(q, backend=wh, tracer=Tracer())
        assert v.decision == "block" and "cartesian" in v.reason


# ---- Column registry on BigQuery ---------------------------------------------

class RegistryClient:
    """Fake client for the registry build: a table, a view, a geography column."""

    def __init__(self, dry_bytes=10 * 1024**2):
        self.dry_bytes = dry_bytes
        self.queries = []
        f = lambda n, t, m="NULLABLE": SimpleNamespace(name=n, field_type=t, mode=m)  # noqa: E731
        self.tables = {
            "orders": SimpleNamespace(table_type="TABLE", num_rows=1000, schema=[
                f("order_id", "INTEGER"), f("status", "STRING"), f("created_at", "TIMESTAMP")]),
            "users": SimpleNamespace(table_type="VIEW", num_rows=0, schema=[
                f("id", "INTEGER"), f("user_geom", "GEOGRAPHY"), f("tags", "STRING", "REPEATED")]),
        }

    def list_tables(self, ds):
        return [SimpleNamespace(table_id=t, reference=t) for t in self.tables]

    def get_table(self, ref):
        return self.tables[ref]

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        assert job_config.maximum_bytes_billed
        if job_config.dry_run:
            return SimpleNamespace(total_bytes_processed=self.dry_bytes)
        if ".orders`" in sql:
            row = (1000, 990, 1000, "1", "1000", 5, 1000, None, None,
                   600, 1000, "2019-01-01 00:00:00+00", "2024-01-01 00:00:00+00")
        else:
            row = (100000, 98000, 100000, "1", "100000")
        return SimpleNamespace(total_bytes_processed=self.dry_bytes,
                               result=lambda timeout=None: [row])


def test_bigquery_registry_build_and_lookup(tmp_path):
    from cost_guard.column_registry import build_bigquery_registry, load_registry

    path = str(tmp_path / "reg.duckdb")
    client = RegistryClient()
    rep = build_bigquery_registry("p", "ds", registry_path=path, client=client)
    assert sorted(rep.profiled) == ["orders", "users"] and rep.columns == 4
    assert rep.bytes_processed == 2 * client.dry_bytes
    # every scan was dry-run first, and nothing ran without a bytes cap
    assert [bool(c.dry_run) for _, c in client.queries] == [True, False, True, False]
    # geography and repeated columns are not profiled
    assert not any("user_geom" in q or "tags" in q for q, _ in client.queries)
    reg = load_registry(path, default_schema="ds")
    users_id = reg.lookup("users", "id")
    # a view is profiled through the view: its rows come from the scan, not the API's 0
    assert users_id.total_rows == 100000 and users_id.distinct_count == 98000
    assert reg.lookup("orders", "created_at").min_value.startswith("2019-01-01")
    assert reg.lookup("orders", "status").min_value is None  # no range for strings
    # stale mode: fresh entries are not re-scanned
    again = build_bigquery_registry("p", "ds", registry_path=path, client=RegistryClient())
    assert again.up_to_date == ["orders", "users"] and again.bytes_processed == 0


def test_bigquery_registry_size_gate_uses_dry_run_bytes(tmp_path):
    from cost_guard.column_registry import build_bigquery_registry

    client = RegistryClient(dry_bytes=5 * 1024**3)
    rep = build_bigquery_registry("p", "ds", registry_path=str(tmp_path / "r.duckdb"),
                                  maximum_bytes_billed=2 * 1024**3, client=client)
    assert rep.profiled == [] and len(rep.oversized) == 2
    assert all(c.dry_run for _, c in client.queries)  # nothing executed


def test_backend_reads_the_dataset_registry(tmp_path):
    from cost_guard.column_registry import build_bigquery_registry

    path = str(tmp_path / "reg.duckdb")
    build_bigquery_registry("p", "ds", registry_path=path, client=RegistryClient())
    wh = BigQueryWarehouse("p", "ds", client=FakeClient(0), registry_path=path)
    assert wh.column_registry().lookup("orders", "order_id").distinct_count == 990
    assert BigQueryWarehouse("p", "ds", client=FakeClient(0),
                             registry_path=str(tmp_path / "none.duckdb")).column_registry() is None


def test_guard_reads_bigquery_sql_in_the_bigquery_dialect():
    """Backticked names are everyday BigQuery SQL; the DuckDB parser rejects
    them. The Guard parses with the backend's dialect, per decision."""
    from cost_guard import sql_ast

    wh = BigQueryWarehouse("proj", client=FakeClient(0))
    v = decide("SELECT `status`, COUNT(*) AS n FROM `proj.thelook_ecommerce.orders` "
               "WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY) "
               "GROUP BY `status`", backend=wh, tracer=Tracer())
    assert v.decision == "allow", v.reason
    v = decide("SELECT * EXCEPT (status) FROM `proj.thelook_ecommerce.orders`",
               backend=wh, tracer=Tracer())
    assert any("full scan" in f for f in v.findings)  # table resolved through backticks
    assert sql_ast.current_dialect() == "duckdb"  # restored after the decision
    with pytest.raises(sql_ast.SQLParseError):
        sql_ast.parse_one("SELECT `status` FROM orders")  # DuckDB by default
