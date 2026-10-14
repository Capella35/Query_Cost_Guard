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
    def __init__(self, nbytes):
        self.nbytes = nbytes
        self.configs = []

    def query(self, sql, job_config=None):
        self.configs.append(job_config)
        return SimpleNamespace(total_bytes_processed=self.nbytes, result=lambda timeout=None: [])

    def get_table(self, ref):
        return SimpleNamespace(
            num_rows=125_226, num_bytes=10_000_000,
            schema=[SimpleNamespace(name=n) for n in ("order_id", "user_id", "created_at")],
            time_partitioning=SimpleNamespace(field="created_at"), clustering_fields=None,
        )


def test_dry_run_estimate_and_bytes_cap():
    fake = FakeClient(nbytes=2 * 1024**4)  # 2 TB > 1 TB interactive cap
    wh = BigQueryWarehouse("proj", client=fake)
    est = wh.estimate("SELECT order_id FROM orders")
    assert est.source == "dry_run" and est.estimated_bytes_scanned == 2 * 1024**4
    assert fake.configs[0].dry_run and fake.configs[0].maximum_bytes_billed
    v = decide("SELECT order_id FROM orders WHERE created_at >= '2024-01-01'",
               backend=wh, tracer=Tracer())
    assert v.decision == "block" and "bytes scanned" in v.reason
    sched = decide("SELECT order_id FROM orders WHERE created_at >= '2024-01-01'",
                   "scheduled", backend=wh, tracer=Tracer())
    assert sched.decision != "block"  # 5 TB scheduled cap


def test_metadata_and_watchdog_config():
    fake = FakeClient(nbytes=0)
    wh = BigQueryWarehouse("proj", client=fake)
    md = wh.table_metadata("orders")
    assert md["partitioned_by"] == "created_at" and md["column_count"] == 3
    assert wh.execute("SELECT 1", timeout_sec=2).status == "completed"
    assert int(fake.configs[-1].job_timeout_ms) == 2000
