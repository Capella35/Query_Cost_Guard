"""BigQuery backend: the stretch goal. Same interface as DuckDBWarehouse.

Cost model: exact bytes via dry run (free, nothing executes) — the Guard
checks them against max_bytes_scanned for the workload class. Every
executing call (execute, column_profile) carries maximum_bytes_billed, and
execute() carries job_timeout_ms (the BigQuery watchdog: the job is
cancelled server-side at the deadline).

Status: unit-tested with a fake client (tests/test_bigquery_backend.py);
the check path (estimate, table_metadata, column_registry) has run live
against dry runs (scripts/bq_live_check.py); execute() has not. sampled() is
not built, so compare_results fails closed on this backend (no rewrite is
ever offered unverified). The Guard parses SQL with the DuckDB dialect; the
30-query set adapted by to_bigquery() parsed without errors, other
BigQuery-only syntax is untested.

Set maximum_bytes_billed on the project BEFORE running anything (especially Q13).
"""
from __future__ import annotations

import time
from typing import Optional

from .base import ColumnProfile, ExecutionResult, PlanEstimate


class BigQueryWarehouse:
    name = "bigquery"

    def __init__(
        self,
        project: str,
        dataset: str = "thelook_ecommerce",
        maximum_bytes_billed: int = 1_099_511_627_776,
        client=None,
        registry_path: Optional[str] = None,
    ):
        from ..column_registry import bigquery_registry_path

        self.project = project
        self.dataset = dataset
        self.maximum_bytes_billed = maximum_bytes_billed
        self._client = client
        # Local profiling lookup (scripts/build_column_registry.py --bigquery-project).
        self.registry_path = registry_path or bigquery_registry_path(project, dataset)
        self._registry = None
        self._registry_loaded = False

    def _bq(self):
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self.project)
        return self._client

    def _job_config(self, **kw):
        from google.cloud import bigquery

        return bigquery.QueryJobConfig(
            default_dataset=f"{self.project}.{self.dataset}",
            maximum_bytes_billed=self.maximum_bytes_billed,
            **kw,
        )

    def estimate(self, sql: str) -> PlanEstimate:
        job = self._bq().query(sql, job_config=self._job_config(dry_run=True, use_query_cache=False))
        nbytes = int(job.total_bytes_processed or 0)
        return PlanEstimate(
            estimated_rows_scanned=0,  # dry run reports bytes, not rows
            estimated_bytes_scanned=nbytes,
            estimated_peak_rows=None,
            source="dry_run",
            detail=f"dry run: {nbytes:,} bytes processed",
        )

    def run_sample(self, sql: str, max_rows: int = 10_000) -> str:
        raise NotImplementedError("use sampled(); not built for BigQuery yet")

    def sampled(self, tables: list[str], rows_per_table: int):
        # Plan: CREATE TEMP TABLE t AS SELECT * FROM t TABLESAMPLE SYSTEM (n PERCENT)
        # inside a script session, with maximum_bytes_billed on every job.
        raise NotImplementedError("sampled verification not built for BigQuery — fails closed")

    def execute(self, sql: str, timeout_sec: Optional[float] = None) -> ExecutionResult:
        kw = {"job_timeout_ms": int(timeout_sec * 1000)} if timeout_sec else {}
        t0 = time.perf_counter()
        try:
            job = self._bq().query(sql, job_config=self._job_config(**kw))
            rows = list(job.result(timeout=timeout_sec))
            return ExecutionResult("completed", time.perf_counter() - t0, len(rows))
        except Exception as e:  # noqa: BLE001
            dur = time.perf_counter() - t0
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            if "timeout" in msg.lower() or "timed out" in msg.lower():
                return ExecutionResult("killed", dur, detail=f"watchdog: {msg}")
            return ExecutionResult("error", dur, detail=msg)

    def table_metadata(self, table: str) -> dict:
        t = self._bq().get_table(f"{self.project}.{self.dataset}.{table}")
        # A view (or external table) reports num_rows 0: unknown, not empty.
        # Passed through as 0 it made users/products "provably tiny" and
        # silenced the cartesian rule (eval/RESULTS_bq_live_check.md).
        known = getattr(t, "table_type", "TABLE") == "TABLE" and t.num_rows is not None
        return {
            "table": table,
            "num_rows": int(t.num_rows) if known else None,
            "num_bytes": int(t.num_bytes or 0),
            "column_count": len(t.schema),
            "columns": [f.name for f in t.schema],
            "partitioned_by": _partition_column(t),
            "clustered_by": list(t.clustering_fields or []),
        }

    def column_registry(self):
        """The local column registry for this dataset, read once and cached.
        None when it has not been built (grouped columns then show up in
        skipped_rules)."""
        if not self._registry_loaded:
            from ..column_registry import load_registry

            self._registry = load_registry(self.registry_path, default_schema=self.dataset)
            self._registry_loaded = True
        return self._registry

    def column_profile(self, table: str, column: str) -> ColumnProfile:
        sql = (
            f"SELECT COUNT(*), APPROX_COUNT_DISTINCT(`{column}`), "
            f"COUNTIF(`{column}` IS NULL) / NULLIF(COUNT(*), 0) FROM `{table}`"
        )
        n, ndv, null_frac = list(self._bq().query(sql, job_config=self._job_config()).result())[0]
        return ColumnProfile(table, column, int(ndv or 0), float(null_frac or 0.0), int(n))


def _partition_column(t) -> Optional[str]:
    """The column a filter must name for BigQuery to prune partitions:
    the time-partitioning column, _PARTITIONTIME for ingestion-time
    partitioning (no column), or the integer-range partitioning column."""
    if t.time_partitioning is not None:
        return t.time_partitioning.field or "_PARTITIONTIME"
    rp = getattr(t, "range_partitioning", None)
    return rp.field if rp is not None else None
