"""Read-only, capped sample execution. Used by compare_results.

Safety: rejects anything that is not read-only (sql_ast.read_only_violation),
runs against a sampled copy of the inputs (at most max_rows rows per input
table) under a timeout, and never returns rows to the caller — only a hash.
"""
from __future__ import annotations

from .. import sql_ast


def run_sample_query(
    sql: str,
    max_rows: int = 10_000,
    backend=None,
    timeout_sec: float = 10.0,
    float_tolerance: float = 1e-6,
) -> str:
    """Run the query on sampled inputs; return the canonical result hash."""
    if backend is None:
        raise ValueError("run_sample_query needs a WarehouseBackend")
    reason = sql_ast.read_only_violation(sql)
    if reason:
        raise PermissionError(f"refused: {reason}")
    tree = sql_ast.parse_one(sql)
    with backend.sampled(sql_ast.referenced_tables(tree), max_rows) as sample:
        return sample.run(sql, sql_ast.has_order_by(tree), float_tolerance, timeout_sec).result_hash
