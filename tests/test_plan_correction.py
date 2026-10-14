"""EXPLAIN estimates corrected with column-registry distinct counts."""
import duckdb

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


def test_range_filters_use_the_value_range(synthetic_wh):
    for q in ("SELECT order_id FROM orders WHERE created_at >= '2023-06-01' AND created_at < '2023-06-08'",
              "SELECT order_id FROM orders WHERE created_at >= '2024-06-01'",
              "SELECT order_id FROM orders WHERE amount_cents BETWEEN 10 AND 100"):
        est = synthetic_wh.estimate(q)
        assert est.source == "explain+corrected"
        assert abs(est.rows_processed - _actual(synthetic_wh, q)) <= 0.05 * _actual(synthetic_wh, q) + 50


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
