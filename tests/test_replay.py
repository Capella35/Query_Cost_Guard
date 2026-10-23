"""Tests for the 70/30 replay harness logic: chronological split, cold start,
the anomaly rule, and the absolute-duration floor. Pure logic — no DuckDB,
so these stay fast."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

from run_replay import anomaly_flag, split_chronological
from query_preflight.thresholds import BaselineStore


def make_stats(durations, rows=1000):
    store = BaselineStore()
    for d in durations:
        store.record("SELECT a FROM t WHERE x = 1", rows, d)
    return store.baseline("SELECT a FROM t WHERE x = 1")


def test_split_is_chronological():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    log = [{"ts": base + timedelta(hours=i), "sql": f"q{i}"} for i in range(100)]
    log.reverse()  # shuffle the input order
    history, test = split_chronological(log, 0.70)
    assert len(history) == 70 and len(test) == 30
    assert max(r["ts"] for r in history) <= min(r["ts"] for r in test)


def test_anomaly_cold_start():
    stats = make_stats([0.1] * 10)  # only 10 runs < 20
    assert anomaly_flag(stats, 1000, 5.0, min_runs=20) is None


def test_anomaly_median_clause():
    # Heavy-tailed history: p95 (0.6) is far above the median (0.1), so the
    # median clause is the one that fires.
    stats = make_stats([0.10] * 18 + [0.5, 0.6])
    assert anomaly_flag(stats, 1000, 0.35, min_runs=20) is not None  # > 3x median
    assert anomaly_flag(stats, 1000, 0.25, min_runs=20) is None  # < 3x, < p95


def test_anomaly_p95_clause():
    stats = make_stats([0.10] * 18 + [0.5, 0.6])
    assert anomaly_flag(stats, 1000, 0.65, min_runs=20) is not None  # above p95


def test_anomaly_absolute_floor():
    stats = make_stats([0.001] * 20)  # 1 ms median point lookups
    # 3 ms is >3x the median, but below the noise floor: never an anomaly.
    assert anomaly_flag(stats, 1000, 0.003, min_runs=20) is None


def test_anomaly_rows_clause():
    stats = make_stats([0.10] * 20, rows=1000)
    # duration fine, but row estimate far above history -> still flagged.
    assert anomaly_flag(stats, 100_000, 0.10, min_runs=20) is not None
