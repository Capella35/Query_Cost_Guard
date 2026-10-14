"""Column registry: a data-profiling lookup table, built once, read per query.

Instead of scanning a column with APPROX_COUNT_DISTINCT every time a query
groups by it, a scheduled job profiles each table once and stores one row per
column in a `column_registry` table in a LOCAL DuckDB file next to the
warehouse (data/thelook.duckdb -> data/thelook.registry.duckdb). The Guard
reads that file (once per backend, cached) and never profiles on the query
path.

Why local, not in the warehouse: the build job only READS the warehouse
(no write access, no exclusive lock), and on a billed warehouse reading the
registry costs nothing — a registry table in BigQuery would be a billed
query (10 MB minimum) on every Guard start. Storage was never the cost (one
row per column); the profiling scan still reads the data once per refresh.

Build mechanics:
- one scan per table covers all its columns (HyperLogLog distinct counts,
  NULL counts, total rows, and min/max for numeric and date/time columns —
  the value range the plan estimator uses for range filters);
- size gate: tables above max_table_rows are not scanned — they are reported,
  so a huge table is a deliberate decision, never an accident;
- every scan runs under scan_timeout_sec;
- refresh modes: "missing" profiles tables with unregistered columns only;
  "stale" also re-profiles tables whose entries are older than max_age_days
  (so a scheduled run actually refreshes drifting counts); "all" re-profiles
  everything;
- each table's rows are replaced atomically (delete + insert in one
  transaction), and rows for dropped tables/columns are removed.

Keyed by (schema_name, table_name, column_name). Entries older than
max_age_days are treated by the Guard as unverified (rule skipped, visibly).
"""
from __future__ import annotations

import threading
from pathlib import Path
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Optional

from .backends.base import ColumnProfile

REGISTRY_TABLE = "column_registry"
HIGH_CARDINALITY_RATIO = 0.5  # Guard rule: distinct/rows at or above = near-unique
DEFAULTS = {
    "max_age_days": 35,  # older entries are unverified
    "max_table_rows": 100_000_000,  # size gate: larger tables are not scanned
    "scan_timeout_sec": 900,
    "use_approximate": True,  # HyperLogLog; exact COUNT(DISTINCT) per column is costly
}


def default_registry_path(db_path: str) -> str:
    """Local registry file for a warehouse file: x.duckdb -> x.registry.duckdb."""
    p = Path(db_path)
    return str(p.with_name(p.stem + ".registry.duckdb"))


def registry_settings(customer: Optional[dict] = None) -> dict:
    """Settings from customer.yaml `column_registry`, over the defaults."""
    return {**DEFAULTS, **((customer or {}).get("column_registry") or {})}


@dataclass
class RegistryEntry:
    schema_name: str
    table_name: str
    column_name: str
    data_type: str
    total_rows: int
    distinct_count: int  # NULLs excluded; capped at the non-NULL rows
    null_count: int
    is_approximate: bool  # distinct_count came from HyperLogLog (~2% error)
    profiled_at: str  # ISO-8601 UTC
    # Value range as text, castable back to data_type. Numeric and date/time
    # columns only (NULL otherwise, or when every value is NULL).
    min_value: Optional[str] = None
    max_value: Optional[str] = None

    @property
    def distinct_ratio(self) -> float:
        return self.distinct_count / self.total_rows if self.total_rows else 0.0

    def as_profile(self) -> ColumnProfile:
        return ColumnProfile(
            table=self.table_name,
            column=self.column_name,
            approx_distinct=self.distinct_count,
            null_fraction=self.null_count / self.total_rows if self.total_rows else 0.0,
            row_count=self.total_rows,
        )

    def age_days(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - datetime.fromisoformat(self.profiled_at)) / timedelta(days=1)


_RANGE_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT",
                "USMALLINT", "UINTEGER", "UBIGINT", "FLOAT", "DOUBLE", "REAL", "DECIMAL",
                "DATE", "TIMESTAMP")


def has_value_range(data_type: str) -> bool:
    """Numeric and date/time types: where a min/max range means something."""
    return data_type.upper().startswith(_RANGE_TYPES)


@dataclass
class BuildReport:
    profiled: list[str] = field(default_factory=list)  # tables scanned this run
    columns: int = 0
    up_to_date: list[str] = field(default_factory=list)  # registered and fresh: not scanned
    oversized: list[str] = field(default_factory=list)  # size gate: not scanned
    failed: list[str] = field(default_factory=list)  # scan error or timeout
    removed_rows: int = 0  # rows for dropped tables/columns


def _profile_table(con, schema: str, table: str, approximate: bool, now: str,
                   timeout_sec: Optional[float]) -> list[RegistryEntry]:
    """Every column of one table in a single scan."""
    cols = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [schema, table],
    ).fetchall()
    if not cols:
        return []
    distinct = "APPROX_COUNT_DISTINCT" if approximate else "COUNT(DISTINCT"
    exprs = ["COUNT(*)"]
    for c, dtype in cols:
        q = '"' + c.replace('"', '""') + '"'
        exprs += [f"{distinct}({q})" + ("" if approximate else ")"), f"COUNT({q})"]
        if has_value_range(dtype):
            exprs += [f"CAST(MIN({q}) AS VARCHAR)", f"CAST(MAX({q}) AS VARCHAR)"]
        else:
            exprs += ["NULL", "NULL"]
    timer = threading.Timer(timeout_sec, con.interrupt) if timeout_sec else None
    if timer:
        timer.start()
    try:
        row = con.execute(f'SELECT {", ".join(exprs)} FROM "{schema}"."{table}"').fetchone()
    finally:
        if timer:
            timer.cancel()
    total = int(row[0])
    out = []
    for i, (c, dtype) in enumerate(cols):
        base = 1 + 4 * i
        non_null = int(row[base + 1] or 0)
        out.append(RegistryEntry(
            schema_name=schema, table_name=table, column_name=c, data_type=dtype,
            total_rows=total,
            distinct_count=min(int(row[base] or 0), non_null),  # HLL can overshoot
            null_count=total - non_null,
            is_approximate=approximate,
            profiled_at=now,
            min_value=row[base + 2],
            max_value=row[base + 3],
        ))
    return out


def build_registry(
    db_path: str,
    tables: Optional[list[str]] = None,
    settings: Optional[dict] = None,
    mode: str = "stale",
    schema: str = "main",
    registry_path: Optional[str] = None,
) -> BuildReport:
    """Create/refresh the local registry for a DuckDB warehouse. The
    warehouse is opened READ-ONLY; only the registry file is written.

    mode: "missing" | "stale" | "all" (see module docstring).
    tables: restrict to these tables (default: every base table in `schema`).
    registry_path: default default_registry_path(db_path).
    """
    import duckdb

    cfg = {**DEFAULTS, **(settings or {})}
    if mode not in ("missing", "stale", "all"):
        raise ValueError(f"mode must be missing|stale|all, got {mode!r}")
    report = BuildReport()
    registry_path = registry_path or default_registry_path(db_path)
    Path(registry_path).parent.mkdir(parents=True, exist_ok=True)
    wh = duckdb.connect(db_path, read_only=True)
    reg = duckdb.connect(registry_path)
    try:
        sql_type = {"bool": "BOOLEAN", "int": "BIGINT"}
        cols_sql = ", ".join(f"{f.name} {sql_type.get(f.type, 'VARCHAR')}"
                             for f in fields(RegistryEntry))
        existing = [r[0] for r in reg.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
            [REGISTRY_TABLE]).fetchall()]
        if existing and existing != [f.name for f in fields(RegistryEntry)]:
            # Different shape (older version): rebuild rather than ALTER —
            # old rows can't be keyed safely.
            reg.execute(f"DROP TABLE {REGISTRY_TABLE}")
        reg.execute(f"CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} ({cols_sql})")

        catalog = wh.execute(
            "SELECT c.table_name, c.column_name FROM information_schema.columns c "
            "JOIN information_schema.tables t USING (table_schema, table_name) "
            "WHERE c.table_schema = ? AND t.table_type = 'BASE TABLE' AND c.table_name <> ?",
            [schema, REGISTRY_TABLE],  # skip a registry table left by older versions
        ).fetchall()
        live: dict[str, set[str]] = {}
        for t, c in catalog:
            live.setdefault(t, set()).add(c)

        # Remove rows for tables/columns that no longer exist.
        registered = reg.execute(
            f"SELECT table_name, column_name, profiled_at FROM {REGISTRY_TABLE} WHERE schema_name = ?",
            [schema],
        ).fetchall()
        gone = [(t, c) for t, c, _ in registered if c not in live.get(t, set())]
        for t, c in gone:
            reg.execute(f"DELETE FROM {REGISTRY_TABLE} WHERE schema_name = ? AND table_name = ? "
                        "AND column_name = ?", [schema, t, c])
        report.removed_rows = len(gone)

        have: dict[str, set[str]] = {}
        oldest: dict[str, str] = {}
        for t, c, ts in registered:
            if (t, c) in gone:
                continue
            have.setdefault(t, set()).add(c)
            oldest[t] = min(oldest.get(t, ts), ts)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg["max_age_days"])).isoformat()

        targets = sorted(live) if tables is None else [t for t in tables if t in live]
        for t in targets:
            missing = live[t] - have.get(t, set())
            stale = t in oldest and oldest[t] < cutoff
            if not (mode == "all" or missing or (mode == "stale" and stale)):
                report.up_to_date.append(t)
                continue
            rows = wh.execute("SELECT estimated_size FROM duckdb_tables() "
                              "WHERE schema_name = ? AND table_name = ?", [schema, t]).fetchone()
            if rows and rows[0] is not None and rows[0] > cfg["max_table_rows"]:
                report.oversized.append(f"{t} (~{rows[0]:,} rows)")
                continue
            now = datetime.now(timezone.utc).isoformat()
            try:
                entries = _profile_table(wh, schema, t, cfg["use_approximate"], now,
                                         cfg["scan_timeout_sec"])
            except Exception as e:  # noqa: BLE001 — one bad table must not stop the run
                report.failed.append(f"{t}: {str(e).splitlines()[0][:100]}")
                continue
            # Replace this table's rows atomically: a failure cannot leave
            # columns deleted and not replaced.
            reg.execute("BEGIN TRANSACTION")
            try:
                reg.execute(f"DELETE FROM {REGISTRY_TABLE} WHERE schema_name = ? AND table_name = ?",
                            [schema, t])
                placeholders = ", ".join("?" for _ in fields(RegistryEntry))
                reg.executemany(f"INSERT INTO {REGISTRY_TABLE} VALUES ({placeholders})",
                                [tuple(asdict(e).values()) for e in entries])
                reg.execute("COMMIT")
            except Exception:
                reg.execute("ROLLBACK")
                raise
            report.profiled.append(t)
            report.columns += len(entries)
        return report
    finally:
        wh.close()
        reg.close()


def load_registry(registry_path: str) -> Optional["ColumnRegistry"]:
    """Read a local registry file; None when it hasn't been built."""
    import duckdb

    if not Path(registry_path).exists():
        return None
    con = duckdb.connect(registry_path, read_only=True)
    try:
        exists = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_schema = 'main' AND table_name = '{REGISTRY_TABLE}'"
        ).fetchone()[0]
        if not exists:
            return None
        cur = con.execute(f"SELECT * FROM {REGISTRY_TABLE}")
        names = [d[0] for d in cur.description]
        return ColumnRegistry.from_rows(cur.fetchall(), names)
    finally:
        con.close()


class ColumnRegistry:
    """In-memory view of the registry table: (schema, table, column) -> entry."""

    def __init__(self, entries: list[RegistryEntry]):
        self._by_key = {
            (e.schema_name.lower(), e.table_name.lower(), e.column_name.lower()): e
            for e in entries
        }

    def __len__(self) -> int:
        return len(self._by_key)

    def entries(self) -> list[RegistryEntry]:
        return list(self._by_key.values())

    def lookup(self, table: str, column: str, schema: str = "main") -> Optional[RegistryEntry]:
        return self._by_key.get((schema.lower(), table.lower(), column.lower()))

    @classmethod
    def from_rows(cls, rows: list[tuple], names: list[str]) -> "ColumnRegistry":
        """Rows read back from the registry table, matched by column name."""
        known = {f.name for f in fields(RegistryEntry)}
        return cls([RegistryEntry(**{k: v for k, v in zip(names, r) if k in known})
                    for r in rows])
