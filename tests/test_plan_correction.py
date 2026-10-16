"""EXPLAIN estimates corrected with column-registry distinct counts."""
import duckdb

import pytest

from cost_guard.backends.duckdb_backend import DuckDBWarehouse
from cost_guard.column_registry import ColumnRegistry, RegistryEntry, build_registry
from cost_guard.guard import decide
from cost_guard.plan_estimate import plan_cardinality
from cost_guard.tracing import Tracer

WINDOW = ("SELECT status, SUM(amount_cents) OVER (PARTITION BY status) FROM orders "
          "WHERE created_at >= '2024-01-01'")
SELF_JOIN = "SELECT a.user_id FROM events a JOIN events b ON a.user_id = b.user_id"


def _entry(table, column, distinct, rows=1_000_000, nulls=0, age_days=0):
    from datetime import datetime, timedelta, timezone

    ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    return RegistryEntry("main", table, column, "VARCHAR", rows, distinct, nulls, True, ts)


def _scan(table, cols, card):
    return {"name": "SEQ_SCAN", "children": [],
            "extra_info": {"Table": f"db.main.{table}", "Projections": cols,
                           "Estimated Cardinality": str(card)}}


def _window_plan():
    """Shape DuckDB produces for SUM(...) OVER (PARTITION BY status)."""
    group = {"name": "HASH_GROUP_BY",
             "extra_info": {"Groups": "#0", "Aggregates": "sum(#1)", "Estimated Cardinality": "100000"},
             "children": [{"name": "PROJECTION",
                           "extra_info": {"Projections": ["__internal_compress_string_uhugeint(#0)", "#1"],
                                          "Estimated Cardinality": "200000"},
                           "children": [_scan("orders", ["status", "amount"], 200_000)]}]}
    proj = {"name": "PROJECTION", "children": [group],
            "extra_info": {"Projections": ["__internal_decompress_string(#0)", "#1"],
                           "Estimated Cardinality": "100000"}}
    return [{"name": "HASH_JOIN", "children": [_scan("orders", "status", 200_000), proj],
             "extra_info": {"Join Type": "INNER", "Conditions": "status IS NOT DISTINCT FROM #0",
                            "Estimated Cardinality": "22573363"}}]


def test_group_bound_traced_through_wrappers_and_join_recomputed():
    reg = ColumnRegistry([_entry("orders", "status", 2)])
    card = plan_cardinality(_window_plan(), reg)
    assert card.peak_uncorrected == 22_573_363
    assert card.peak == 200_000  # 200k x 3 groups / max(3, 3)
    assert any("GROUP BY orders.status: at most 3 groups" in c for c in card.corrections)


def test_no_registry_or_stale_entry_means_no_correction():
    assert plan_cardinality(_window_plan(), None).peak == 22_573_363
    stale = ColumnRegistry([_entry("orders", "status", 2, age_days=90)])
    card = plan_cardinality(_window_plan(), stale)
    assert card.peak == 22_573_363 and card.corrections == []


def test_nulls_add_a_group_and_bound_never_raises():
    reg = ColumnRegistry([_entry("orders", "status", 2, nulls=10)])
    plan = _window_plan()[0]["children"][1]["children"][0]  # the GROUP BY alone
    assert plan_cardinality([plan], reg).peak == 200_000  # scan dominates
    grp_only = dict(plan, extra_info=dict(plan["extra_info"], **{"Estimated Cardinality": "2"}))
    card = plan_cardinality([grp_only], reg)
    assert card.corrections == []  # planner's 2 is already below the bound (3 + 1 NULL)


def test_join_with_uncorrected_inputs_keeps_planner_estimate():
    reg = ColumnRegistry([_entry("events", "user_id", 10)])  # would shrink it if applied
    plan = [{"name": "HASH_JOIN",
             "children": [_scan("events", "user_id", 1_000_000), _scan("events", "user_id", 1_000_000)],
             "extra_info": {"Join Type": "INNER", "Conditions": "user_id = user_id",
                            "Estimated Cardinality": "21100607"}}]
    card = plan_cardinality(plan, reg)
    assert card.peak == 21_100_607 and card.corrections == []


# ---- end to end on DuckDB ---------------------------------------------------

def test_window_query_no_longer_falsely_blocked(synthetic_wh):
    est = synthetic_wh.estimate(WINDOW)
    assert est.source == "explain+corrected" and est.corrections
    # ~half the synthetic orders match created_at >= 2024-01-01; two scans.
    assert 400_000 <= est.estimated_peak_rows <= 600_000
    assert est.rows_processed <= 1_200_000
    v = decide(WINDOW, backend=synthetic_wh, tracer=Tracer())
    assert v.decision != "block" and v.estimate_source == "explain+corrected"


def test_real_blowups_still_block(synthetic_wh):
    for q in (SELF_JOIN, "SELECT * FROM users, orders"):
        est = synthetic_wh.estimate(q)
        assert est.source == "explain" and not est.corrections
        assert decide(q, backend=synthetic_wh, tracer=Tracer()).decision == "block"


def test_without_registry_window_still_blocks(tmp_path):
    db = str(tmp_path / "noreg.duckdb")
    con = duckdb.connect(db)
    con.execute("CREATE TABLE orders AS SELECT range AS order_id, "
                "CASE WHEN range % 10 = 0 THEN 'cancelled' ELSE 'complete' END AS status, "
                "10 + range % 490 AS amount_cents, DATE '2023-01-01' + (range % 730)::INTEGER "
                "AS created_at FROM range(1000000)")
    con.close()
    est = DuckDBWarehouse(db).estimate(WINDOW)
    assert est.source == "explain" and est.rows_processed > 5_000_000
    build_registry(db)
    assert DuckDBWarehouse(db).estimate(WINDOW).source == "explain+corrected"


# ---- value ranges (min/max) -------------------------------------------------

def _actual(wh, q):
    return wh._connect().execute(f"SELECT COUNT(*) FROM ({q})").fetchone()[0]


def test_registry_stores_value_ranges(synthetic_wh):
    reg = synthetic_wh.column_registry()
    created = reg.lookup("orders", "created_at")
    assert (created.min_value, created.max_value) == ("2023-01-01", "2024-12-30")
    assert reg.lookup("orders", "amount_cents").min_value == "10"
    assert reg.lookup("orders", "status").min_value is None  # text: no range


def test_wide_range_raises_the_flat_estimate(synthetic_wh):
    # ~29% of orders match; the planner's flat guess is 20% -> raised.
    q = "SELECT order_id FROM orders WHERE created_at >= '2024-06-01'"
    est = synthetic_wh.estimate(q)
    assert est.source == "explain+corrected"
    assert any("raised" in c for c in est.corrections)
    assert abs(est.rows_processed - _actual(synthetic_wh, q)) <= 0.05 * _actual(synthetic_wh, q)


def test_narrow_range_never_lowers_the_estimate(synthetic_wh):
    # A 7-day window is ~1% of the range: the planner's 20% stands (an
    # overestimate is the safe direction for a cap).
    for q in ("SELECT order_id FROM orders WHERE created_at >= '2023-06-01' AND created_at < '2023-06-08'",
              "SELECT order_id FROM orders WHERE amount_cents BETWEEN 10 AND 100"):
        est = synthetic_wh.estimate(q)
        assert not any("value range" in c for c in est.corrections)
        assert est.rows_processed >= _actual(synthetic_wh, q)


def test_filter_dropped_as_always_true_counts_every_row(synthetic_wh):
    q = "SELECT order_id FROM orders WHERE created_at >= '2023-01-01' AND created_at < '2025-01-01'"
    est = synthetic_wh.estimate(q)
    assert est.rows_processed == 1_000_000 == _actual(synthetic_wh, q)
    assert any("no filter left in the plan" in c for c in est.corrections)


def test_no_range_claims_outside_the_profiled_range_or_for_non_ranges(synthetic_wh):
    for q in ("SELECT order_id FROM orders WHERE created_at >= '2030-01-01'",
              "SELECT order_id FROM orders WHERE status = 'complete'",
              "SELECT order_id FROM orders WHERE created_at >= '2024-01-01' OR amount_cents > 400"):
        assert not any("value range" in c for c in synthetic_wh.estimate(q).corrections)


def test_two_way_range_estimate_is_opt_in(thelook_wh):
    """The caps see the raise-only estimate; the anomaly rule's two-way
    estimate also lowers a narrow range to its min/max fraction."""
    week = ("SELECT order_id FROM orders WHERE created_at >= TIMESTAMP '2021-03-01' "
            "AND created_at < TIMESTAMP '2021-03-08'")
    two_years = ("SELECT order_id FROM orders WHERE created_at >= TIMESTAMP '2021-01-01' "
                 "AND created_at < TIMESTAMP '2023-01-01'")
    raise_only = thelook_wh.estimate(week).rows_processed
    two_way = thelook_wh.estimate(week, two_way_ranges=True)
    assert two_way.rows_processed < raise_only / 10
    assert any("LOWERED" in c for c in two_way.corrections)
    # The Guard (and so every cap) keeps the raise-only estimate.
    assert decide(week, backend=thelook_wh, tracer=Tracer()).estimated_rows == raise_only
    # Separation between a narrow and a wide window of the same shape.
    wide = thelook_wh.estimate(two_years, two_way_ranges=True).rows_processed
    assert wide / two_way.rows_processed > 50
    assert wide / raise_only < 3  # what the raise-only estimate shows


# ---- Date parts as DuckDB prints them in EXPLAIN ----------------------------------

@pytest.mark.parametrize("conjunct, expected", [
    # EXTRACT(YEAR ...), date_part('year', ...) and year(...) all print as "year"(col)
    ('("year"(created_at) = 2023)', ("created_at", "2023-01-01", "2024-01-01")),
    ('"year"("created_at")>=2022', ("created_at", "2022-01-01", None)),
    ('("year"(created_at) > 2022)', ("created_at", "2023-01-01", None)),
    ('("year"(created_at) <= 2020)', ("created_at", None, "2021-01-01")),
    ("(date_part('year', created_at) < 2021)", ("created_at", None, "2021-01-01")),
    ("(CAST(created_at AS DATE) BETWEEN '2023-01-01'::DATE AND '2023-01-31'::DATE)",
     ("created_at", "2023-01-01", "2023-02-01")),
    ("(created_at::DATE = '2023-03-01')", ("created_at", "2023-03-01", "2023-03-02")),
    ("(CAST(\"created_at\" AS DATE) > '2023-03-01'::DATE)", ("created_at", "2023-03-02", None)),
    # no single range, or not a date part
    ('("month"(created_at) = 6)', None),
    ('("year"(created_at) = 2023 OR status = \'x\')', None),
    ("created_at>='2023-01-01 08:00:00+00'::TIMESTAMP WITH TIME ZONE", None),
])
def test_temporal_range_reads_plan_text(conjunct, expected):
    from cost_guard.backends.duckdb_backend import temporal_range

    assert temporal_range(conjunct) == expected


def test_split_conjuncts_respects_strings_and_parentheses():
    from cost_guard.backends.duckdb_backend import _split_conjuncts

    assert _split_conjuncts("(\"year\"(created_at) = 2023) AND (status = 'A AND B') "
                            "AND (x BETWEEN 1 AND 2)") == [
        '("year"(created_at) = 2023)', "(status = 'A AND B')", "(x BETWEEN 1 AND 2)"]


def test_year_and_date_cast_filters_get_a_range_estimate(thelook_wh):
    """Before, these plan filters were not recognized as ranges, so the
    two-way estimate kept the planner's flat guess (486k on events)."""
    for q, real_low, real_high in [
        ("SELECT COUNT(*) FROM events WHERE CAST(created_at AS DATE) "
         "BETWEEN DATE '2023-01-01' AND DATE '2023-01-31'", 20_000, 120_000),
        ("SELECT COUNT(*) FROM events WHERE EXTRACT(YEAR FROM created_at) = 2023", 300_000, 1_200_000),
    ]:
        est = thelook_wh.estimate(q, two_way_ranges=True)
        assert any("range filter on created_at" in c for c in est.corrections), est.corrections
        assert real_low < est.rows_processed < real_high


def _registry_for(entries):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    return ColumnRegistry([RegistryEntry("main", t, c, dtype, 1000, 900, 0, True, now, lo, hi)
                           for t, c, dtype, lo, hi in entries])


def test_date_column_missing_from_the_registry_gets_no_range(thelook_wh):
    """A date column added after the registry was last built: the plan text
    is a perfectly good date part, but nothing confirms the column's type or
    range, so the planner's estimate stands."""
    reg = _registry_for([("events", "user_id", "BIGINT", None, None)])  # no created_at yet
    assert thelook_wh._range_fraction(reg, "main", "events", '("year"(created_at) = 2023)') is None


def test_dates_stored_as_text_get_no_range(thelook_wh):
    """Dates kept in a VARCHAR column: DuckDB accepts CAST(col AS DATE) and
    prints it in the plan, but the registry types the column as text — its
    min/max are strings, not a time range."""
    reg = _registry_for([("events", "event_day", "VARCHAR", "2019-01-01", "2024-01-31")])
    f = "(CAST(event_day AS DATE) BETWEEN '2023-01-01'::DATE AND '2023-01-31'::DATE)"
    assert thelook_wh._range_fraction(reg, "main", "events", f) is None
    # the same filter on a column the registry types as a date is a range
    reg = _registry_for([("events", "event_day", "DATE", "2019-01-01", "2024-01-31")])
    assert thelook_wh._range_fraction(reg, "main", "events", f) is not None


# ---- Identifier columns are objects, not numbers -----------------------------------

def test_registry_stores_no_value_range_for_identifiers(tmp_path):
    db = str(tmp_path / "w.duckdb")
    con = duckdb.connect(db)
    con.execute("CREATE TABLE t AS SELECT range AS id, range % 50 AS user_id, "
                "range AS amount, TIMESTAMP '2024-01-01' + INTERVAL (range) MINUTE AS created_at "
                "FROM range(2000)")
    con.close()
    build_registry(db, registry_path=str(tmp_path / "r.duckdb"))
    from cost_guard.column_registry import load_registry

    reg = load_registry(str(tmp_path / "r.duckdb"))
    for c in ("id", "user_id"):  # objects: distinct counts yes, min/max no
        e = reg.lookup("t", c)
        assert e.is_identifier and e.min_value is None and e.distinct_count > 0
    assert reg.lookup("t", "amount").min_value == "0"  # a real number keeps its range
    assert reg.lookup("t", "created_at").min_value is not None


def test_range_filter_on_an_identifier_is_not_a_value_range(thelook_wh):
    """user_id < 500 says nothing about how many rows match, even when an older
    registry still holds a min/max for it."""
    reg = _registry_for([("orders", "user_id", "BIGINT", "1", "100000"),
                         ("orders", "num_of_item", "BIGINT", "1", "4")])
    assert thelook_wh._range_fraction(reg, "main", "orders", "user_id<500") is None
    assert thelook_wh._range_fraction(reg, "main", "orders", "num_of_item<=2") is not None
