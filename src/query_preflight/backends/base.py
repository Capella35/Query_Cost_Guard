"""Warehouse backend interface.

Primary backend: DuckDB (local, TheLook Kaggle CSVs, zero cloud spend).
Stretch goal: BigQuery (real bytes via dry-run).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ContextManager, Optional, Protocol


@dataclass
class PlanEstimate:
    estimated_rows_scanned: int
    estimated_bytes_scanned: Optional[int]  # None on DuckDB — rows are the cost proxy
    detail: str = ""
    # Largest intermediate row count anywhere in the plan (join fan-out,
    # cartesian products). None when the backend can't see the plan shape.
    estimated_peak_rows: Optional[int] = None
    # "explain" (engine cardinality estimates), "explain+corrected" (plan
    # estimates corrected: registry bounds, dropped filters, value ranges), "dry_run" (BigQuery bytes), or
    # "table_sum_fallback" (sum of referenced table sizes — blind to fan-out).
    source: str = "explain"
    corrections: list = field(default_factory=list)  # what the registry corrected, and why
    # Rows into the largest full sort (ORDER BY with no LIMIT) in the plan;
    # 0 = no full sort, None = the backend can't see the plan shape.
    estimated_sorted_rows: Optional[int] = None

    @property
    def rows_processed(self) -> int:
        """What the rows cap is checked against: the worse of scan and peak."""
        return max(self.estimated_rows_scanned or 0, self.estimated_peak_rows or 0)


@dataclass
class ColumnProfile:
    """What the warehouse knows about a column that the analyst usually doesn't."""

    table: str
    column: str
    approx_distinct: int
    null_fraction: float
    row_count: int


@dataclass
class SampleRun:
    """One query run against sampled inputs. Hash only — never rows."""

    result_hash: str
    row_count: int
    elapsed_sec: float


@dataclass
class ExecutionResult:
    status: str  # "completed" | "killed" | "error"
    duration_sec: float
    row_count: int = 0
    detail: str = ""
    bytes_billed: Optional[int] = None  # BigQuery job's billed bytes; None elsewhere


class SampledInputs(Protocol):
    """Sampled copies of a query's input tables; both sides of a comparison
    run against the SAME sample."""

    input_rows_sampled: int
    input_rows_full: int

    def run(self, sql: str, ordered: bool, float_tolerance: float,
            timeout_sec: Optional[float]) -> SampleRun: ...


class WarehouseBackend(Protocol):
    name: str

    def estimate(self, sql: str) -> PlanEstimate:
        """Cost estimate WITHOUT executing the query (dry-run or EXPLAIN)."""
        ...

    def run_sample(self, sql: str, max_rows: int = 10_000) -> str:
        """Run a capped sample; return the result hash — never rows."""
        ...

    def sampled(self, tables: list[str], rows_per_table: int,
                focus: Optional[dict] = None) -> ContextManager[SampledInputs]:
        """Sampled copies of `tables` for bounded-cost verification. `focus`
        ({table: (column, lo_sql, hi_sql or None)}, from
        compare_results.date_windows) asks for every row of a short date
        window to be in the sample; a backend may ignore it."""
        ...

    def execute(self, sql: str, timeout_sec: Optional[float],
                max_bytes_billed: Optional[int] = None) -> ExecutionResult:
        """Run the query with a watchdog; kill it past timeout_sec. A backend
        that bills bytes refuses the query past max_bytes_billed."""
        ...

    def table_metadata(self, table: str) -> dict:
        """Size/shape metadata for the static rules."""
        ...

    def column_registry(self):
        """Pre-built column profiles (column_registry.ColumnRegistry), or None
        when the registry has not been built. The Guard reads this instead
        of profiling columns per query."""
        ...

    def column_profile(self, table: str, column: str) -> ColumnProfile:
        """Live profile of one column (a scan). Not used per query."""
        ...
