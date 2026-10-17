"""Adaptive anomaly rule: a run far above its OWN query shape's history.

Moved here from eval/run_replay.py (which imports it back, so the replays
and production run the same code). Evidence, all from the replays:
- size clause (rows or bytes above the shape's p95, or > 3x its median):
  separates bad windows only with a two-way size estimate (x104 vs x2
  raise-only; RESULTS_replay_bq.md) — DuckDB: estimate(two_way_ranges=True),
  BigQuery: dry-run bytes (exact);
- runtime clause, drift-aware (thresholds.DriftAwareBaseline): on the hard
  replay's pre-registered held-out seeds, false positives 50.5% -> 1.1%,
  catch 80% -> 72.5% (RESULTS_replay_hard.md).

Production use (runtime.run_query): after a run completes, check it against
the completed runs of the same fingerprint in the query log. It never
blocks or kills anything — the hard caps and the watchdog do that; a flag
is logged on the run and handed to the report-only investigator.
Cold start: no flag below COLD_START_RUNS runs of history.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .thresholds import DriftAwareBaseline, FingerprintStats

COLD_START_RUNS = 20  # brief: static rules only below ~20 runs per fingerprint
ANOMALY_MEDIAN_MULT = 3.0  # brief: "above p95 or a few times the median"
MIN_ABS_DURATION_SEC = 0.02  # runtime is noisy at ms scale (caching, jitter):
    # the duration clause only fires above this floor, so a 3 ms point lookup
    # can never be flagged as an anomaly over a 1 ms median
REGRESSION_RATIO = 1.5  # = investigator.REGRESSION_RATIO; the drift clause's min ratio


def anomaly_flag(stats: FingerprintStats | None, rows_scanned, duration_sec,
                 min_runs: int = COLD_START_RUNS) -> str | None:
    """Layer 2 anomaly rule (brief §Layer 2): warn when far above the
    fingerprint's own history — above p95 or a few times the median.
    Cold start: needs >= min_runs history runs.

    rows_scanned is the DuckDB upper-bound row estimate, standing in for
    bytes scanned on this path (the brief's dry-run bytes on BigQuery)."""
    if stats is None or stats.run_count < min_runs:
        return None
    reasons = []
    if rows_scanned is not None:
        pb, mb = stats.p95_bytes, stats.median_bytes
        if (pb is not None and rows_scanned > pb) or (
            mb and rows_scanned > ANOMALY_MEDIAN_MULT * mb
        ):
            reasons.append(
                f"rows scanned {rows_scanned:,} above fingerprint history "
                f"(median {mb:,.0f}, p95 {pb:,.0f}, {stats.run_count} runs)"
            )
    if duration_sec is not None and duration_sec > MIN_ABS_DURATION_SEC:
        ps, ms = stats.p95_secs, stats.median_secs
        if (ps is not None and duration_sec > ps) or (
            ms and duration_sec > ANOMALY_MEDIAN_MULT * ms
        ):
            reasons.append(
                f"runtime {duration_sec:.2f}s above fingerprint history "
                f"(median {ms:.2f}s, p95 {ps:.2f}s, {stats.run_count} runs)"
            )
    if reasons:
        return "adaptive anomaly: " + "; ".join(reasons)
    return None


def drift_aware_flag(stats: FingerprintStats | None, baseline, rows_scanned, t_h: float,
                     duration_sec: float | None, min_runs: int = COLD_START_RUNS) -> str | None:
    """The anomaly rule with a drift-aware runtime clause: the size clause is
    unchanged; runtime is compared with the shape's trend-expected runtime
    (thresholds.DriftAwareBaseline) instead of its flat median/p95."""
    if stats is None or stats.run_count < min_runs:
        return None
    size = anomaly_flag(stats, rows_scanned, None, min_runs)  # size clause only
    if size:
        return size
    if (baseline is not None and duration_sec is not None and duration_sec > MIN_ABS_DURATION_SEC
            and baseline.is_anomaly(t_h, duration_sec, REGRESSION_RATIO)):
        return (f"adaptive anomaly: runtime {duration_sec:.2f}s is "
                f"{baseline.excess(t_h, duration_sec):.1f}x the shape's trend-expected "
                f"{baseline.expected(t_h):.2f}s ({baseline.runs} runs, trend "
                f"{'used' if baseline.trend_trusted else 'not trusted: flat'})")
    return None


@dataclass
class Anomaly:
    fingerprint: str
    kind: str  # "size" | "runtime"
    reason: str
    duration_sec: float
    size: Optional[int]
    history_runs: int
    history_median_sec: Optional[float]


def _hours(ts: str, t0: datetime) -> float:
    return (datetime.fromisoformat(ts) - t0).total_seconds() / 3600


def check_run(log: list[dict], fingerprint: str, ts: str, size: Optional[int],
              duration_sec: float, min_runs: int = COLD_START_RUNS,
              exclude_flagged: bool = False) -> Optional[Anomaly]:
    """Check one completed run against the completed runs of its shape that
    came before it in the log (records with `anomaly_size`; older records
    without it have no comparable size and are left out). Drift-aware rule.

    exclude_flagged: leave runs that were themselves flagged out of the
    history ("option B"), so one spike does not raise the bar for the next.
    False keeps every run ("option A", what the batch replays evaluated)."""
    hist = sorted((r for r in log if r["fingerprint"] == fingerprint and r["status"] == "completed"
                   and r.get("duration_sec") is not None and r.get("anomaly_size") is not None
                   and r["ts"] < ts and not (exclude_flagged and r.get("anomaly"))),
                  key=lambda r: r["ts"])
    if len(hist) < min_runs:
        return None
    stats = FingerprintStats(fingerprint, [(r["anomaly_size"], r["duration_sec"]) for r in hist])
    t0 = datetime.fromisoformat(hist[0]["ts"])
    baseline = DriftAwareBaseline.fit([_hours(r["ts"], t0) for r in hist],
                                      [r["duration_sec"] for r in hist])
    # Extrapolate the trend at most one history span past the last run. The
    # replays had continuous traffic; in production a shape can pause for days,
    # and a trend fitted on 25 minutes of noise then "expected" 8e7 s 16 days
    # later (found by test), so nothing could ever be flagged.
    last_h = _hours(hist[-1]["ts"], t0)
    t_h = min(_hours(ts, t0), last_h + max(last_h, 1e-9))
    reason = drift_aware_flag(stats, baseline, size, t_h, duration_sec, min_runs)
    if reason is None:
        return None
    return Anomaly(fingerprint=fingerprint,
                   kind="size" if reason.startswith("adaptive anomaly: rows") else "runtime",
                   reason=reason, duration_sec=duration_sec, size=size,
                   history_runs=len(hist), history_median_sec=stats.median_secs)
