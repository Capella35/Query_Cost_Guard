"""BigQuery backend: the stretch goal. Same interface as DuckDBWarehouse.

Cost model: exact bytes via dry run (free, nothing executes) — the Guard
checks them against max_bytes_scanned for the workload class. Every
executing call (execute, column_profile) carries maximum_bytes_billed, and
execute() carries job_timeout_ms (the BigQuery watchdog: the job is
cancelled server-side at the deadline).

Status: unit-tested with a fake client (tests/test_bigquery_backend.py);
the check path (estimate, table_metadata, column_registry) has run live
against dry runs (scripts/bq_live_check.py); execute() has not. sampled()
verifies rewrites on TEMP-table samples in a BigQuery session (real, small,
capped queries). The Guard parses SQL with the DuckDB dialect; the
30-query set adapted by to_bigquery() parsed without errors, other
BigQuery-only syntax is untested.

Set maximum_bytes_billed on the project BEFORE running anything (especially Q13).
"""
from __future__ import annotations

import copy
import re
import threading
import time
from contextlib import contextmanager
from typing import Optional

from sqlglot import exp

from ..results import result_hash
from .base import ColumnProfile, ExecutionResult, PlanEstimate, SampleRun

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAMPLE_SETUP_TIMEOUT_SEC = 120
# Table layouts and a column's full size rarely change: cached this long, so a
# check makes one dry run instead of a metadata call per table plus extra dry
# runs (measured: ~200-300 ms per table, ~1.8 s for a column's full size).
METADATA_TTL_SEC = 600


class _BigQuerySample:
    """Sampled copies of input tables as TEMP tables in a BigQuery session.
    Queries run inside the session with table names stripped of their
    project/dataset, so they read the samples."""

    def __init__(self, wh, session_id: str, tables: list[str], sampled: int, full: int):
        self._wh, self._session, self._tables = wh, session_id, set(tables)
        self.input_rows_sampled, self.input_rows_full = sampled, full

    def run(self, sql: str, ordered: bool, float_tolerance: float = 1e-6,
            timeout_sec: Optional[float] = None) -> SampleRun:
        from .. import sql_ast

        tree = sql_ast.parse_one(sql, "bigquery").copy()
        for t in tree.find_all(exp.Table):
            if t.name.lower() in self._tables:
                t.set("catalog", None)
                t.set("db", None)
        kw = {"job_timeout_ms": int(timeout_sec * 1000)} if timeout_sec else {}
        t0 = time.perf_counter()
        job = self._wh._bq().query(tree.sql(dialect="bigquery"),
                                   job_config=self._wh._session_config(self._session, **kw))
        rows = [tuple(r.values()) for r in job.result(timeout=timeout_sec)]
        return SampleRun(result_hash=result_hash(rows, ordered=ordered, float_tolerance=float_tolerance),
                         row_count=len(rows), elapsed_sec=time.perf_counter() - t0)



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
        self._cache: dict = {}  # key -> (expires_at, value); see _cached
        self._cache_lock = threading.Lock()

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

    def partition_fraction(self, table: str, column: str, conditions) -> Optional[float]:
        """Share of `table`'s partitions that `conditions` (predicates on its
        partition column, as sqlglot expressions) read: dry-run bytes of the
        column with the filter / without. Free; None when unknown."""
        if not (_IDENT.match(table) and _IDENT.match(column)):
            return None
        where = " AND ".join(f"({c.sql(dialect='bigquery')})" for c in conditions)
        src = f"`{self.project}.{self.dataset}.{table}`"
        try:
            part = self.estimate(f"SELECT `{column}` FROM {src} WHERE {where}").estimated_bytes_scanned
            whole = self._cached(("column_bytes", table, column), lambda: self.estimate(
                f"SELECT `{column}` FROM {src}").estimated_bytes_scanned)
        except Exception:  # noqa: BLE001 — unknown fraction: the estimate stays "filtered"
            return None
        return part / whole if whole else None

    def run_sample(self, sql: str, max_rows: int = 10_000) -> str:
        raise NotImplementedError("use sampled(); not built for BigQuery yet")

    def _session_config(self, session_id: Optional[str] = None, **kw):
        from google.cloud import bigquery

        if session_id is None:
            kw["create_session"] = True
        else:
            kw["connection_properties"] = [bigquery.ConnectionProperty("session_id", session_id)]
        # No default dataset: bare names must resolve to the session's TEMP tables.
        return bigquery.QueryJobConfig(maximum_bytes_billed=self.maximum_bytes_billed,
                                       use_query_cache=False, **kw)

    @contextmanager
    def sampled(self, tables: list[str], rows_per_table: int):
        """TEMP-table samples of each input table in one BigQuery session;
        both sides of a comparison read the SAME copies. Tables are sampled
        with TABLESAMPLE SYSTEM (billed for the sampled blocks only, about
        rows_per_table rows); views and small tables are copied with LIMIT
        (any shared copy works for an equivalence check). Every job carries
        maximum_bytes_billed; the session is aborted afterwards."""
        client = self._bq()
        registry = self.column_registry()
        stmts, full = [], 0
        for t in tables:
            if not _IDENT.match(t):
                raise ValueError(f"unsafe table name {t!r}")
            meta = client.get_table(f"{self.project}.{self.dataset}.{t}")
            n = int(meta.num_rows) if getattr(meta, "table_type", "TABLE") == "TABLE" else None
            if n is None and registry is not None:
                e = next((e for e in registry.entries() if e.table_name.lower() == t.lower()), None)
                n = int(e.total_rows) if e is not None else None
            full += n or 0
            src = f"`{self.project}.{self.dataset}.{t}`"
            if getattr(meta, "table_type", "TABLE") == "TABLE" and n and n > rows_per_table:
                pct = min(100.0, 100.0 * rows_per_table / n)
                body = f"SELECT * FROM {src} TABLESAMPLE SYSTEM ({pct:.4f} PERCENT)"
            else:
                body = f"SELECT * FROM {src} LIMIT {int(rows_per_table)}"
            stmts.append(f"CREATE TEMP TABLE `{t}` AS {body};")
        setup = client.query("\n".join(stmts), job_config=self._session_config(
            job_timeout_ms=SAMPLE_SETUP_TIMEOUT_SEC * 1000))
        setup.result(timeout=SAMPLE_SETUP_TIMEOUT_SEC)
        session = setup.session_info.session_id
        try:
            counts = " + ".join(f"(SELECT COUNT(*) FROM `{t}`)" for t in tables) or "0"
            sampled = list(client.query(f"SELECT {counts}",
                                        job_config=self._session_config(session)).result())[0][0]
            yield _BigQuerySample(self, session, [t.lower() for t in tables], int(sampled), full)
        finally:
            try:
                client.query("CALL BQ.ABORT_SESSION()",
                             job_config=self._session_config(session)).result()
            except Exception:  # noqa: BLE001 — an unclosed session expires on its own
                pass

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

    def _cached(self, key, compute):
        """`compute()` once per METADATA_TTL_SEC per key; thread-safe. A
        failure is not cached."""
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]
        value = compute()
        with self._cache_lock:
            self._cache[key] = (now + METADATA_TTL_SEC, value)
        return value

    def table_metadata(self, table: str) -> dict:
        # A copy: callers annotate the dict they get (the Guard does).
        return copy.deepcopy(self._cached(("table", table), lambda: self._table_metadata(table)))

    def _table_metadata(self, table: str) -> dict:
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

    def column_kinds(self) -> dict:
        """Column-kind labels (column_kinds.py) next to the registry, read once:
        {"table.column": {kind, reviewed, ...}}; {} when none were made."""
        if getattr(self, "_kinds", None) is None:
            from ..column_kinds import kinds_path, load_kinds

            self._kinds = load_kinds(kinds_path(self.registry_path))
        return self._kinds

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
