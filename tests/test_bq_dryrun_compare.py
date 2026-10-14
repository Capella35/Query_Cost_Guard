"""scripts/bq_dryrun_compare.py: DuckDB -> BigQuery SQL adaptation and the
dry-run comparison, against a fake client (no GCP project)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("google.cloud.bigquery")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from bq_dryrun_compare import compare, to_bigquery  # noqa: E402


@pytest.mark.parametrize("duck, expected", [
    # bare TIMESTAMP column vs DuckDB TIMESTAMP literal -> BigQuery TIMESTAMP (not DATETIME)
    ("SELECT 1 FROM orders WHERE created_at >= TIMESTAMP '2023-06-01'",
     "created_at >= CAST('2023-06-01' AS TIMESTAMP)"),
    # the sargable rewrite: DATE bounds + INTERVAL folded into TIMESTAMP literals
    ("SELECT 1 FROM orders WHERE (created_at >= CAST('2023-11-24' AS DATE) "
     "AND created_at < CAST('2023-11-24' AS DATE) + INTERVAL '1' DAY)",
     "created_at < CAST('2023-11-25' AS TIMESTAMP)"),
    ("SELECT 1 FROM events WHERE created_at < CAST('2023-12-01' AS TIMESTAMP) + INTERVAL '1' MONTH",
     "created_at < CAST('2024-01-01' AS TIMESTAMP)"),
    # CAST(col AS DATE) = X keeps its DATE literal
    ("SELECT 1 FROM orders WHERE CAST(created_at AS DATE) = DATE '2023-03-01'",
     "CAST(created_at AS DATE) = CAST('2023-03-01' AS DATE)"),
    # DATE_TRUNC on the column: TIMESTAMP_TRUNC with a TIMESTAMP literal
    ("SELECT 1 FROM orders WHERE DATE_TRUNC('month', created_at) = TIMESTAMP '2023-06-01'",
     "TIMESTAMP_TRUNC(created_at, MONTH) = CAST('2023-06-01' AS TIMESTAMP)"),
])
def test_to_bigquery(duck, expected):
    out = to_bigquery(duck)
    assert expected in out
    assert "DATETIME" not in out


def test_literal_only_comparison_is_folded():
    out = to_bigquery("SELECT 1 FROM t WHERE x = 1 AND DATE_TRUNC('MONTH', CAST('2023-06-01' AS DATE)) "
                      "= CAST('2023-06-01' AS DATE)")
    assert "TRUE" in out and "TRUNC" not in out
    out = to_bigquery("SELECT 1 FROM t WHERE x = 1 AND DATE_TRUNC('MONTH', CAST('2023-06-15' AS DATE)) "
                      "= CAST('2023-06-15' AS DATE)")
    assert "FALSE" in out


class FakeClient:
    """Bytes by marker: a query mentioning '2023-03-02' (a one-day range) prunes."""

    def query(self, sql, job_config=None):
        assert job_config.dry_run and job_config.maximum_bytes_billed
        if "nosuchtable" in sql:
            raise RuntimeError("404 Not found: Table nosuchtable")
        return SimpleNamespace(total_bytes_processed=10 * 1024**2 if "2023-03-02" in sql else 1024**3)


def test_compare_reports_bytes_saved_and_errors():
    cases = [
        ("X1", "deterministic:sargable",
         "SELECT COUNT(*) FROM orders WHERE CAST(created_at AS DATE) = DATE '2023-03-01'",
         "SELECT COUNT(*) FROM orders WHERE (created_at >= CAST('2023-03-01' AS DATE) "
         "AND created_at < CAST('2023-03-01' AS DATE) + INTERVAL '1' DAY)"),
        ("E1", "deterministic:cte", "SELECT 1 FROM nosuchtable", "SELECT 1 FROM orders"),
    ]
    rows = compare(FakeClient(), cases, "p.d", 20 * 1024**3, price_per_tb=6.25)
    x1 = rows[0]
    assert x1["original_bytes"] == 1024**3 and x1["rewrite_bytes"] == 10 * 1024**2
    assert x1["saved_pct"] == pytest.approx(99.0, abs=0.1)
    assert x1["modeled_usd_saved_per_run"] == round((1024**3 - 10 * 1024**2) / 1024**4 * 6.25, 6)
    assert rows[1]["original_error"].startswith("404") and "saved_pct" not in rows[1]


def test_shift_dates_keeps_month_alignment_and_pairs():
    from datetime import date

    from bq_dryrun_compare import months_to_target, shift_dates

    orig = "SELECT 1 FROM orders WHERE DATE_TRUNC('month', created_at) = TIMESTAMP '2023-06-01'"
    rew = ("SELECT 1 FROM orders WHERE (created_at >= CAST('2023-06-01' AS TIMESTAMP) AND "
           "created_at < CAST('2023-06-01' AS TIMESTAMP) + INTERVAL '1' MONTH)")
    k = months_to_target(orig, date(2026, 9, 1))
    assert k == 39
    assert "2026-09-01" in shift_dates(orig, k)
    assert "2026-10-01" in to_bigquery(shift_dates(rew, k))  # same shift for the rewrite
    assert shift_dates(orig, 0) == orig
