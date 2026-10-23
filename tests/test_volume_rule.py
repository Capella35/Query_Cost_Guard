"""Volume rule (guard._volume_finding): a cheap query shape run many times is
judged by its total. Warns, never blocks; BigQuery dry-run bytes only."""
from datetime import datetime, timedelta, timezone

from query_preflight.backends.bigquery_backend import BigQueryWarehouse
from query_preflight.config import load_customer
from query_preflight.runtime import LogRecord, append_log, read_log, run_query, shape_volume
from query_preflight.thresholds import fingerprint_query
from query_preflight.tracing import Tracer
from test_bigquery_backend import FakeClient

GIB = 1024**3
Q = "SELECT order_id FROM orders WHERE created_at >= '2024-01-01'"


def _log(path, n, billed, hours_ago=1.0, sql=Q):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    for _ in range(n):
        append_log(LogRecord(ts=ts, fingerprint=fingerprint_query(sql), workload_class="interactive",
                             decision="allow", status="completed", duration_sec=1.0,
                             rows_estimate=None, timeout_sec=60, bytes_estimate=billed,
                             bytes_billed=billed), path)


def _run(path, nbytes=50 * GIB):
    wh = BigQueryWarehouse("proj", client=FakeClient(nbytes=nbytes))
    return run_query(Q, wh, customer=load_customer(), log_path=path, tracer=Tracer())


def test_warns_when_the_shape_total_passes_the_threshold(tmp_path):
    path = tmp_path / "log.jsonl"
    _log(path, 19, 50 * GIB)  # 19 x 50 GiB + this run = 1,000 GiB, ~$6.10 at $6.25/TiB
    out = _run(path)
    assert out.verdict.decision == "warn"
    (f,) = [f for f in out.verdict.findings if "query shape ran" in f]
    assert "20 times in the last 24 h" in f and "MODELED" in f and "summary table" in f


def test_silent_below_min_runs_or_threshold(tmp_path):
    few = tmp_path / "few.jsonl"
    _log(few, 5, 500 * GIB)  # expensive, but only 6 runs: the caps' job, not this rule's
    assert not any("query shape ran" in f for f in _run(few).verdict.findings)
    cheap = tmp_path / "cheap.jsonl"
    _log(cheap, 100, 10 * 1024**2)  # 100 runs at the 10 MiB minimum: ~$0.006
    assert not any("query shape ran" in f for f in _run(cheap, nbytes=1024**2).verdict.findings)


def test_cache_hits_and_old_runs_do_not_count(tmp_path):
    path = tmp_path / "log.jsonl"
    _log(path, 30, 0)  # cache hits bill 0
    _log(path, 30, 50 * GIB, hours_ago=30)  # outside the 24 h window
    vol = shape_volume(read_log(path), fingerprint_query(Q))
    assert vol.runs == 30 and vol.bytes == 0
    assert not any("query shape ran" in f for f in _run(path).verdict.findings)


def test_this_run_counts_bigquerys_minimum_per_table(tmp_path):
    path = tmp_path / "log.jsonl"
    _log(path, 19, 0)
    customer = load_customer()
    customer["volume"] = {"window_hours": 24, "min_runs": 20, "warn_usd_per_window": 0.00005}
    wh = BigQueryWarehouse("proj", client=FakeClient(nbytes=1024))  # 1 KiB read, 10 MiB billed
    out = run_query(Q, wh, customer=customer, log_path=path, tracer=Tracer())
    (f,) = [f for f in out.verdict.findings if "query shape ran" in f]
    assert "~$0.0001 this run" in f  # 10 MiB x $6.25/TiB = $0.0000596


def test_skipped_visibly_without_bigquery_bytes(synthetic_wh, tmp_path):
    out = run_query("SELECT 1", synthetic_wh, customer=load_customer(), log_path=tmp_path / "l.jsonl",
                    tracer=Tracer())
    assert any(s.startswith("volume_cost:") for s in out.verdict.skipped_rules)
