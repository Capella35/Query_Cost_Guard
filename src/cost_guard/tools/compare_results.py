"""Equivalence check for Guard rewrites (F1.4).

Runs original and rewritten queries against the SAME sampled copy of their
input tables (at most sample_rows rows per table, fixed seed), under a
timeout, and compares canonical result hashes. Returns pass/fail + hash —
never rows (private-deployment boundary).

Why sampled inputs and not `LIMIT` on the output: a LIMIT on an outer
wrapper does not bound an aggregate — the full input is still scanned, and
on BigQuery that bills the full query. Sampling the inputs bounds the work.

Canonical comparison (results.py): order-insensitive unless the original
query has a top-level ORDER BY; floats rounded to float_tolerance; NULLs
treated as one consistent value.

Honest limitations:
- Equality on a sample is necessary, not sufficient: a rewrite that differs
  only on rows outside the sample passes.
- Independent per-table samples thin out joins. If BOTH sides return zero
  rows the check is inconclusive and FAILS — an empty-vs-empty match proves
  nothing.
"""
from __future__ import annotations

from .. import sql_ast
from ..interfaces import ComparisonOutcome

DEFAULT_SAMPLE_ROWS = 50_000
DEFAULT_TIMEOUT_SEC = 10.0


def compare_results(
    original_sql: str,
    rewritten_sql: str,
    float_tolerance: float = 1e-6,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    backend=None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> ComparisonOutcome:
    if backend is None:
        raise NotImplementedError("compare_results needs a WarehouseBackend")
    for sql in (original_sql, rewritten_sql):
        reason = sql_ast.read_only_violation(sql)
        if reason:
            return ComparisonOutcome(False, "", f"refused: {reason}")

    a, b = sql_ast.parse_one(original_sql), sql_ast.parse_one(rewritten_sql)
    ordered = sql_ast.has_order_by(a)
    tables = list(dict.fromkeys(sql_ast.referenced_tables(a) + sql_ast.referenced_tables(b)))

    try:
        with backend.sampled(tables, sample_rows) as sample:
            r1 = sample.run(original_sql, ordered, float_tolerance, timeout_sec)
            r2 = sample.run(rewritten_sql, ordered, float_tolerance, timeout_sec)
            sampled, full = sample.input_rows_sampled, sample.input_rows_full
    except Exception as e:  # noqa: BLE001 — timeout or SQL error: fail closed
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        if "interrupt" in msg.lower():
            msg = f"verification exceeded {timeout_sec}s timeout"
        return ComparisonOutcome(False, "", f"verification failed: {msg}")

    frac = sampled / full if full else 1.0
    cost = (
        f"verified on {sampled:,} of {full:,} input rows ({frac:.1%}), "
        f"{r1.elapsed_sec + r2.elapsed_sec:.2f}s"
    )
    if r1.row_count == 0 and r2.row_count == 0:
        return ComparisonOutcome(
            False, r2.result_hash,
            f"inconclusive: both empty on the sample — rewrite rejected; {cost}",
            input_rows_sampled=sampled, input_rows_full=full,
        )
    ok = r1.result_hash == r2.result_hash
    return ComparisonOutcome(
        passed=ok,
        result_hash=r2.result_hash,
        detail=(
            ("sampled result hashes match — rewrite accepted; " if ok
             else "sampled result hashes DIFFER — rewrite rejected; ") + cost
        ),
        input_rows_sampled=sampled,
        input_rows_full=full,
    )
