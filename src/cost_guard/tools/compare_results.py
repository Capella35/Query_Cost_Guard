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

Date windows: a rewrite that filters a table's date column to a short window
(at most FOCUS_MAX_DAYS, an open "since X" counted up to now) asks the backend
to put every row of that window, widened by FOCUS_MARGIN on both sides so a
boundary mistake shows, in the sample (date_windows). Without it a few-day
window was usually missing from a 10% block sample (T14).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import sql_ast
from ..interfaces import ComparisonOutcome

DEFAULT_SAMPLE_ROWS = 50_000
DEFAULT_TIMEOUT_SEC = 10.0
FOCUS_MAX_DAYS = 31  # longer windows: the block sample already hits them
FOCUS_MARGIN = timedelta(days=1)


def _time_literal(x) -> Optional[tuple[datetime, object]]:
    """(value, the literal node) for a TIMESTAMP/DATE literal, typed or bare."""
    from ..rewrites import _literal_time

    when = _literal_time(x)
    return (when, x) if when is not None else None


def _shifted(node, when: datetime) -> str:
    """`when` as a literal of the same type as `node` (a CAST keeps its type)."""
    exp = sql_ast.exp
    text = exp.Literal.string(when.isoformat(sep=" "))
    out = exp.Cast(this=text, to=node.to.copy()) if isinstance(node, exp.Cast) else text
    return out.sql(dialect=sql_ast.current_dialect())


def _flat_conjuncts(cond) -> list:
    """AND-ed terms, looking through parentheses (the sargable rewrites write
    `(col >= lo AND col < hi)`)."""
    exp = sql_ast.exp
    out = []
    for c in sql_ast.conjuncts(cond):
        while isinstance(c, exp.Paren):
            c = c.this
        out.extend(_flat_conjuncts(c) if isinstance(c, exp.And) else [c])
    return out


def date_windows(tree, now: Optional[datetime] = None) -> dict:
    """{table: (column, lo_sql, hi_sql or None)} for each short date window
    the query's WHERE clauses put on one table's column (col >= / > lo,
    optionally col < / <= hi), widened by FOCUS_MARGIN."""
    exp = sql_ast.exp
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    names = {t.alias_or_name.lower(): t.name.lower() for t in tree.find_all(exp.Table)}
    only = next(iter(set(names.values()))) if len(set(names.values())) == 1 else None
    bounds: dict[tuple[str, str], dict] = {}
    for where in tree.find_all(exp.Where):
        for c in _flat_conjuncts(where):
            if not isinstance(c, (exp.GTE, exp.GT, exp.LT, exp.LTE)):
                continue
            col, lit, lower = c.this, c.expression, isinstance(c, (exp.GTE, exp.GT))
            if not isinstance(col, exp.Column):
                col, lit, lower = c.expression, c.this, not lower
            got = _time_literal(lit) if isinstance(col, exp.Column) else None
            if got is None:
                continue
            table = names.get(col.table.lower()) if col.table else only
            if table is None:
                continue
            b = bounds.setdefault((table, col.name.lower()), {})
            key = "lo" if lower else "hi"
            if key not in b or (got[0] > b[key][0] if lower else got[0] < b[key][0]):
                b[key] = got
    out = {}
    for (table, col), b in bounds.items():
        if "lo" not in b or table in out:
            continue
        lo, lo_node = b["lo"]
        hi = b["hi"][0] if "hi" in b else None
        if ((hi or now) - lo).total_seconds() > FOCUS_MAX_DAYS * 86400:
            continue
        out[table] = (col, _shifted(lo_node, lo - FOCUS_MARGIN),
                      _shifted(b["hi"][1], hi + FOCUS_MARGIN) if hi else None)
    return out


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
        focus = date_windows(b)
        # Without a window, the plain call: backends that predate `focus` keep working.
        with (backend.sampled(tables, sample_rows, focus=focus) if focus
              else backend.sampled(tables, sample_rows)) as sample:
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
