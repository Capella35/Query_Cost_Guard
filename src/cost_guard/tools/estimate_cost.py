"""Cost estimation: dry-run (BigQuery) or EXPLAIN (DuckDB fallback).

Returns exact bytes scanned where the warehouse provides them. Runtime prediction
is calibrated from per-fingerprint history — NEVER derived from the dry run
alone. For cold fingerprints the runtime estimate is explicitly unknown (None),
not a fabricated number.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CostEstimate:
    bytes_scanned: int
    runtime_sec: Optional[float]  # None = unknown (cold fingerprint)
    runtime_source: str  # "history" | "unknown"
    fingerprint: str


def estimate_cost(sql: str, backend=None, history=None) -> CostEstimate:
    """Cost estimate for one query via the warehouse backend.

    backend: a WarehouseBackend (estimate without executing).
    history: a BaselineStore — runtime comes from the fingerprint's own past
             runs, and only once it has enough of them (cold start otherwise).
    """
    from ..thresholds import fingerprint_query

    fp = fingerprint_query(sql)
    if backend is None:
        raise NotImplementedError("estimate_cost needs a WarehouseBackend")
    est = backend.estimate(sql)
    runtime, source = None, "unknown"
    if history is not None:
        stats = history.baseline(sql)
        if stats is not None and stats.run_count >= history.min_runs_for_adaptive:
            runtime, source = stats.median_secs, "history"
    return CostEstimate(
        bytes_scanned=est.estimated_bytes_scanned or 0,
        runtime_sec=runtime,
        runtime_source=source,
        fingerprint=fp,
    )
