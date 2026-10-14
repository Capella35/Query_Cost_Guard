"""Column registry: a profiling lookup table, built once, read per query."""
import duckdb
import pytest

from cost_guard.backends.duckdb_backend import DuckDBWarehouse
from cost_guard.column_registry import (
    REGISTRY_TABLE,
    RegistryEntry,
    build_registry,
    default_registry_path,
)
from cost_guard.guard import decide
from cost_guard.tracing import Tracer

GROUP_SESSION = "SELECT session_id, COUNT(*) FROM events GROUP BY session_id"


def _db(tmp_path, rows=200_000):
    db = str(tmp_path / "w.duckdb")
    con = duckdb.connect(db)
    con.execute(f"CREATE TABLE events AS SELECT range AS id, range AS session_id, "
                f"CASE WHEN range % 50 = 0 THEN 'x' END AS promo, "
                f"DATE '2024-01-01' AS created_at FROM range({rows})")
    con.execute("CREATE TABLE tiny AS SELECT 1 AS a")
    con.close()
    return db


def _rows(db):
    con = duckdb.connect(default_registry_path(db), read_only=True)
    try:
        return con.execute(f"SELECT table_name, column_name, distinct_count, null_count, "
                           f"total_rows, profiled_at FROM {REGISTRY_TABLE} ORDER BY 1, 2").fetchall()
    finally:
        con.close()


def test_profiles_every_column(synthetic_wh):
    reg = synthetic_wh.column_registry()
    s = reg.lookup("events", "session_id")
    assert s.total_rows == 1_000_000 and s.distinct_ratio > 0.5 and s.is_approximate
    assert reg.lookup("orders", "nope") is None
    assert all(e.distinct_count <= e.total_rows - e.null_count for e in reg.entries())


def test_null_counts(tmp_path):
    db = _db(tmp_path)
    build_registry(db)
    promo = [r for r in _rows(db) if r[1] == "promo"][0]
    assert promo[3] == 200_000 - 4_000 and promo[2] == 1  # NULLs excluded from distinct


def test_modes_missing_stale_all(tmp_path):
    db = _db(tmp_path)
    rep = build_registry(db, mode="missing")
    assert sorted(rep.profiled) == ["events", "tiny"]
    first = _rows(db)
    rep = build_registry(db, mode="missing")
    assert rep.profiled == [] and sorted(rep.up_to_date) == ["events", "tiny"]
    assert _rows(db) == first
    # A new column on one table: only that table is re-profiled.
    con = duckdb.connect(db)
    con.execute("ALTER TABLE tiny ADD COLUMN b INTEGER DEFAULT 2")
    con.close()
    assert build_registry(db, mode="missing").profiled == ["tiny"]
    # Stale entries are refreshed by mode="stale" (the default), not by "missing".
    assert build_registry(db, settings={"max_age_days": -1}, mode="missing").profiled == []
    assert sorted(build_registry(db, settings={"max_age_days": -1}).profiled) == ["events", "tiny"]
    assert sorted(build_registry(db, mode="all").profiled) == ["events", "tiny"]


def test_size_gate_reports_not_scans(tmp_path):
    db = _db(tmp_path)
    rep = build_registry(db, settings={"max_table_rows": 1_000})
    assert rep.profiled == ["tiny"]
    assert rep.oversized and rep.oversized[0].startswith("events")
    assert not [r for r in _rows(db) if r[0] == "events"]


def test_dropped_columns_are_removed(tmp_path):
    db = _db(tmp_path)
    build_registry(db)
    con = duckdb.connect(db)
    con.execute("ALTER TABLE events DROP COLUMN promo")
    con.close()
    rep = build_registry(db)
    assert rep.removed_rows == 1
    assert not [r for r in _rows(db) if r[1] == "promo"]


def test_guard_uses_registry_without_scanning(synthetic_wh, monkeypatch):
    monkeypatch.setattr(DuckDBWarehouse, "column_profile",
                        lambda *a, **k: pytest.fail("per-query column scan"))
    t = Tracer()
    v = decide(GROUP_SESSION, backend=synthetic_wh, tracer=t)
    assert v.decision == "warn"
    assert any("near-unique column 'session_id'" in f for f in v.findings)
    assert v.skipped_rules == []
    assert any(s["name"] == "column_registry.lookup" for s in t.spans)


def test_missing_registry_is_a_visible_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(DuckDBWarehouse, "column_profile",
                        lambda *a, **k: pytest.fail("per-query column scan"))
    db = _db(tmp_path)
    v = decide(GROUP_SESSION, backend=DuckDBWarehouse(db), tracer=Tracer())
    assert any("events.session_id': no column registry" in s for s in v.skipped_rules)
    build_registry(db)
    v = decide(GROUP_SESSION, backend=DuckDBWarehouse(db), tracer=Tracer())
    assert v.skipped_rules == [] and any("near-unique" in f for f in v.findings)


def test_stale_entry_is_unverified_not_trusted(synthetic_wh, monkeypatch):
    monkeypatch.setattr(RegistryEntry, "age_days", lambda self, now=None: 90.0)
    v = decide(GROUP_SESSION, backend=synthetic_wh, tracer=Tracer())
    assert not any("near-unique" in f for f in v.findings)
    assert any("90 days old (> 35) — unverified" in s for s in v.skipped_rules)


def test_registry_is_local_and_warehouse_is_read_only(tmp_path):
    db = _db(tmp_path)
    # A read-only connection held elsewhere must not block the build: the
    # warehouse is only read.
    holder = duckdb.connect(db, read_only=True)
    try:
        rep = build_registry(db)
    finally:
        holder.close()
    assert sorted(rep.profiled) == ["events", "tiny"]
    assert default_registry_path(db).endswith("w.registry.duckdb")
    con = duckdb.connect(db, read_only=True)
    try:
        tables = [r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()]
    finally:
        con.close()
    assert REGISTRY_TABLE not in tables  # nothing written into the warehouse
    assert DuckDBWarehouse(db).column_registry().lookup("events", "session_id") is not None
