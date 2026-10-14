"""DuckDB backend: the local-first warehouse.

Reads the TheLook Kaggle CSVs via data/thelook.duckdb (built by
scripts/load_thelook_duckdb.py). No account, no billing, no caps needed.

Cost model: estimate() reads DuckDB's own plan (EXPLAIN (FORMAT JSON)) and
takes the engine's per-operator cardinality estimates, so join fan-out and
cartesian products show up as a large peak row count. CROSS_PRODUCT nodes
carry no estimate; their cardinality is the product of their inputs. Where
the column registry bounds a GROUP BY's output, plan_estimate.py corrects
the estimate (and the joins above it); a scan whose filter the planner
dropped as always-true counts every row; a remaining range filter is
estimated from the registry's min/max — labeled source="explain+corrected". If
EXPLAIN fails, the fallback is the old upper bound — the sum of referenced
table sizes — labeled source="table_sum_fallback" because it cannot see
fan-out. Note: DuckDB estimates range filters at a flat selectivity, so a
7-day and a 2-year date window get the same estimate; the Investigator
calibrates real runtime from history.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from contextlib import contextmanager
from typing import Optional

from .. import sql_ast
from ..plan_estimate import plan_cardinality
from ..results import result_hash
from .base import ColumnProfile, ExecutionResult, PlanEstimate, SampleRun

_IDENT = re.compile(r"[A-Za-z_][\w]*")


def referenced_tables(sql: str) -> list[str]:
    """Physical tables the query reads, from the AST (CTE names excluded)."""
    tree = sql_ast.try_parse(sql)
    return sql_ast.referenced_tables(tree) if tree is not None else []


def _check_ident(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(f"bad identifier: {name}")
    return name


class _DuckDBSample:
    """Sampled copies of input tables in a private in-memory database."""

    def __init__(self, con, input_rows_sampled: int, input_rows_full: int):
        self._con = con
        self.input_rows_sampled = input_rows_sampled
        self.input_rows_full = input_rows_full

    def run(self, sql: str, ordered: bool, float_tolerance: float = 1e-6,
            timeout_sec: Optional[float] = None) -> SampleRun:
        timer = threading.Timer(timeout_sec, self._con.interrupt) if timeout_sec else None
        t0 = time.perf_counter()
        if timer:
            timer.start()
        try:
            rows = self._con.execute(sql).fetchall()
        finally:
            if timer:
                timer.cancel()
        return SampleRun(
            result_hash=result_hash(rows, ordered=ordered, float_tolerance=float_tolerance),
            row_count=len(rows),
            elapsed_sec=time.perf_counter() - t0,
        )


class DuckDBWarehouse:
    name = "duckdb"

    def __init__(self, db_path: str = "data/thelook.duckdb", read_only: bool = True,
                 registry_path: Optional[str] = None):
        from ..column_registry import default_registry_path

        self.db_path = db_path
        # Local profiling lookup (not stored in the warehouse).
        self.registry_path = registry_path or default_registry_path(db_path)
        self.read_only = read_only
        self._con = None
        self._row_counts: dict[str, int] = {}
        self._registry = None
        self._registry_loaded = False
        # Registry entries older than this are not used to correct estimates.
        self.registry_max_age_days = 35

    def _connect(self):
        import duckdb

        if self._con is None:
            self._con = duckdb.connect(self.db_path, read_only=self.read_only)
        return self._con

    def _referenced_tables(self, sql: str) -> list[str]:
        return referenced_tables(sql)

    def row_count(self, table: str) -> int:
        if table not in self._row_counts:
            self._row_counts[table] = self._connect().execute(
                f'SELECT COUNT(*) FROM "{_check_ident(table)}"'
            ).fetchone()[0]
        return self._row_counts[table]

    def estimate(self, sql: str) -> PlanEstimate:
        con = self._connect()
        try:
            rows = con.execute("EXPLAIN (FORMAT JSON) " + sql).fetchall()
            plans = json.loads(rows[0][1])
        except Exception as e:  # noqa: BLE001 — any planner failure -> labeled fallback
            return self._fallback_estimate(sql, e)
        try:
            registry = self.column_registry()
        except Exception:  # noqa: BLE001 — no registry: uncorrected EXPLAIN
            registry = None
        card = plan_cardinality(plans, registry, self.registry_max_age_days,
                                scan_hook=self._scan_hook(registry))
        detail = (f"EXPLAIN estimate: ~{card.scanned:,} rows from scans, "
                  f"peak ~{card.peak:,} rows")
        if card.corrections:
            detail += (f" (planner peak {card.peak_uncorrected:,}; corrections: "
                       + "; ".join(card.corrections) + ")")
        return PlanEstimate(
            estimated_rows_scanned=card.scanned,
            estimated_bytes_scanned=None,
            estimated_peak_rows=card.peak,
            source="explain+corrected" if card.corrections else "explain",
            detail=detail,
            corrections=card.corrections,
        )

    def _scan_hook(self, registry):
        """Scan-level corrections for plan_estimate (see its rules)."""

        def hook(schema: str, table: str, filters: Optional[str], orig: int):
            if schema != "main":
                return None
            try:
                rows = self.row_count(table)
            except Exception:  # noqa: BLE001
                return None
            if not filters:
                if orig < rows:
                    return rows, (
                        f"scan of {table}: no filter left in the plan (the planner dropped "
                        f"one it proved always true) but estimated {orig:,} — every row "
                        f"is read: {rows:,}"
                    )
                return None
            found = self._range_fraction(registry, schema, table, filters)
            if found is None:
                return None
            fraction, col = found
            new = max(1, round(rows * fraction))
            if new == orig:
                return None
            return new, (
                f"scan of {table}: range filter on {col} covers ~{fraction:.1%} of its "
                f"value range per the column registry (planner's flat estimate {orig:,}) "
                "— assumes evenly spread values; an estimate, not a bound"
            )

        return hook

    def _range_fraction(self, registry, schema: str, table: str, filters: str):
        """(fraction of rows, column) for a scan filter that is a conjunction
        of range comparisons on ONE column against constants, from the
        registry's min/max. None when it isn't, the entry is missing/stale,
        or the filter lies outside the profiled range (data may have grown
        since profiling — no claim)."""
        import sqlglot
        from sqlglot import exp

        if registry is None:
            return None
        try:
            tree = sqlglot.parse_one(filters, read="duckdb")
        except Exception:  # noqa: BLE001
            return None
        parts = list(tree.flatten()) if isinstance(tree, exp.And) else [tree]
        col, lows, highs = None, [], []
        for c in parts:
            if isinstance(c, exp.Between):
                target, bounds = c.this, [("lo", c.args["low"]), ("hi", c.args["high"])]
            elif isinstance(c, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
                if isinstance(c.this, exp.Column):
                    target, lit, flip = c.this, c.expression, False
                elif isinstance(c.expression, exp.Column):
                    target, lit, flip = c.expression, c.this, True
                else:
                    return None
                lower = isinstance(c, (exp.GT, exp.GTE)) != flip
                bounds = [("lo" if lower else "hi", lit)]
            else:
                return None  # =, IN, OR, LIKE, functions: not a range
            if not isinstance(target, exp.Column) or (col and target.name != col):
                return None
            col = target.name
            for kind, lit in bounds:
                if lit.find(exp.Column) is not None or any(
                    isinstance(n, exp.Func) and not isinstance(n, exp.Cast) for n in lit.walk()
                ):
                    return None  # bound is not a constant
                (lows if kind == "lo" else highs).append(lit.sql(dialect="duckdb"))
        entry = registry.lookup(table, col, schema)
        if (entry is None or entry.min_value is None or entry.max_value is None
                or entry.age_days() > self.registry_max_age_days):
            return None
        temporal = entry.data_type.upper().startswith(("DATE", "TIMESTAMP"))

        def num(sql_expr: str) -> str:
            return f"epoch({sql_expr})" if temporal else f"CAST({sql_expr} AS DOUBLE)"

        typed = f"CAST(? AS {entry.data_type})"
        query = "SELECT " + ", ".join([num(typed), num(typed)] + [num(x) for x in lows + highs])
        try:
            vals = self._connect().execute(query, [entry.min_value, entry.max_value]).fetchone()
        except Exception:  # noqa: BLE001
            return None
        mn, mx = vals[0], vals[1]
        lo_vals, hi_vals = vals[2:2 + len(lows)], vals[2 + len(lows):]
        if any(v > mx for v in lo_vals) or any(v < mn for v in hi_vals):
            return None
        lo, hi = max([mn, *lo_vals]), min([mx, *hi_vals])
        fraction = 1.0 if mx <= mn else max(0.0, min(1.0, (hi - lo) / (mx - mn)))
        if entry.total_rows:
            fraction *= 1 - entry.null_count / entry.total_rows
        return fraction, col

    def _fallback_estimate(self, sql: str, e: Exception) -> PlanEstimate:
        """EXPLAIN failed: sum of referenced table sizes, labeled as such."""
        tables = self._referenced_tables(sql)
        total = 0
        for t in tables:
            try:
                total += self.row_count(t)
            except Exception:  # noqa: BLE001
                pass  # table not loaded; contributes 0
        return PlanEstimate(
            estimated_rows_scanned=total,
            estimated_bytes_scanned=None,
            estimated_peak_rows=None,
            source="table_sum_fallback",
            detail=(
                f"FALLBACK upper bound (EXPLAIN failed: {str(e).splitlines()[0][:80]}): "
                f"full scans of {tables or 'no known tables'} — blind to join fan-out"
            ),
        )

    def run_sample(self, sql: str, max_rows: int = 10_000) -> str:
        """Legacy capped run: LIMIT on an outer wrapper. Does NOT bound the
        work of an aggregate — compare_results uses sampled() instead."""
        con = self._connect()
        rows = con.execute(f"SELECT * FROM ({sql}) AS _sample LIMIT {int(max_rows)}").fetchall()
        return hashlib.sha256(repr(rows).encode()).hexdigest()

    @contextmanager
    def sampled(self, tables: list[str], rows_per_table: int):
        """Private in-memory DB holding a reservoir sample (fixed seed) of
        each input table, at most rows_per_table rows each. Both sides of a
        comparison run against this one sample.

        BigQuery equivalent: TABLESAMPLE SYSTEM (n PERCENT) into a temp
        table, with maximum_bytes_billed on the job.
        """
        import duckdb

        mem = duckdb.connect()
        try:
            path = self.db_path.replace("'", "''")
            mem.execute(f"ATTACH '{path}' AS src (READ_ONLY)")
            sampled = full = 0
            for t in tables:
                t = _check_ident(t)
                n = self.row_count(t)
                full += n
                if n > rows_per_table:
                    mem.execute(
                        f'CREATE TABLE "{t}" AS SELECT * FROM src."{t}" '
                        f"USING SAMPLE reservoir({int(rows_per_table)} ROWS) REPEATABLE (42)"
                    )
                    sampled += rows_per_table
                else:
                    mem.execute(f'CREATE TABLE "{t}" AS SELECT * FROM src."{t}"')
                    sampled += n
            mem.execute("DETACH src")
            yield _DuckDBSample(mem, sampled, full)
        finally:
            mem.close()

    def execute(self, sql: str, timeout_sec: Optional[float] = None) -> ExecutionResult:
        """Runtime watchdog (F4.2): run on a cursor and interrupt it past
        timeout_sec. Callers must only pass read-only queries — killing a
        write mid-flight is not safe; the Guard enforces this."""
        cur = self._connect().cursor()
        timer = threading.Timer(timeout_sec, cur.interrupt) if timeout_sec else None
        t0 = time.perf_counter()
        if timer:
            timer.start()
        try:
            rows = cur.execute(sql).fetchall()
            return ExecutionResult("completed", time.perf_counter() - t0, len(rows))
        except Exception as e:  # noqa: BLE001
            dur = time.perf_counter() - t0
            if "interrupt" in str(e).lower():
                return ExecutionResult("killed", dur, detail=f"watchdog: exceeded {timeout_sec}s")
            return ExecutionResult("error", dur, detail=str(e).splitlines()[0])
        finally:
            if timer:
                timer.cancel()
            cur.close()

    def table_metadata(self, table: str) -> dict:
        con = self._connect()
        columns = [
            r[1] for r in con.execute(f"PRAGMA table_info('{_check_ident(table)}')").fetchall()
        ]
        return {
            "table": table,
            "num_rows": self.row_count(table),
            "column_count": len(columns),
            "columns": columns,
        }

    def column_registry(self):
        """The local column registry (column_registry.py) for this warehouse,
        read once and cached. None when it has not been built."""
        if not self._registry_loaded:
            from ..column_registry import load_registry

            self._registry = load_registry(self.registry_path)
            self._registry_loaded = True
        return self._registry

    def column_profile(self, table: str, column: str) -> ColumnProfile:
        """Live profile of one column (a full column scan). The Guard does
        NOT call this per query — it reads the column registry instead."""
        con = self._connect()
        _check_ident(table), _check_ident(column)
        n, ndv, null_frac = con.execute(
            f'SELECT COUNT(*), APPROX_COUNT_DISTINCT("{column}"), '
            f'AVG(CASE WHEN "{column}" IS NULL THEN 1.0 ELSE 0.0 END) '
            f'FROM "{table}"'
        ).fetchone()
        return ColumnProfile(
            table=table,
            column=column,
            approx_distinct=int(ndv or 0),
            null_fraction=float(null_frac or 0.0),
            row_count=int(n),
        )
