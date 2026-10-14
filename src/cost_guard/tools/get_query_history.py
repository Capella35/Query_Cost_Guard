"""Per-fingerprint run history from the query log. Used mainly by the Investigator.

Source of truth locally: runs/query_log.jsonl, written by runtime.run_query
(fingerprints and numbers only — no SQL text). On BigQuery the same records
come from INFORMATION_SCHEMA.JOBS (not built; stretch goal).

Synthetic-history rule: keep bad patterns RARE (<5% of runs per fingerprint),
or the anomaly rule just rediscovers structure you planted.
"""
from __future__ import annotations

from pathlib import Path


def get_query_history(
    fingerprint: str, since: str | None = None, log_path: str | Path | None = None
) -> list[dict]:
    """Past runs for a fingerprint: duration, status, row estimate, timestamp.
    since: ISO-8601 timestamp; only runs at or after it."""
    from ..runtime import DEFAULT_LOG, read_log

    rows = [r for r in read_log(log_path or DEFAULT_LOG) if r["fingerprint"] == fingerprint]
    if since is not None:
        rows = [r for r in rows if r["ts"] >= since]
    return rows


def baseline_store_from_log(log_path: str | Path | None = None, min_runs: int = 20):
    """Rebuild per-fingerprint baselines from completed runs in the log."""
    from ..runtime import DEFAULT_LOG, read_log
    from ..thresholds import BaselineStore, FingerprintStats

    store = BaselineStore(min_runs_for_adaptive=min_runs)
    for r in read_log(log_path or DEFAULT_LOG):
        if r["status"] != "completed" or r["duration_sec"] is None:
            continue
        store.stats.setdefault(r["fingerprint"], FingerprintStats(r["fingerprint"])).runs.append(
            (r["rows_estimate"] or 0, r["duration_sec"])
        )
    return store
