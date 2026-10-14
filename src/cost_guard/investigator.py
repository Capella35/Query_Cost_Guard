"""Investigator: post-execution learning loop.

Reads the query log, finds recurring expensive patterns, and proposes tighter
thresholds. Each proposal then goes through ThresholdUpdater (thresholds.py):

1. deterministic safety gates (below the absolute hard caps, tighten only,
   bounded movement, enough runs, not below the baseline p95),
2. an optional LLM judge that sees aggregates only and may reject or hold,
   never enforce,
3. a shadow-mode trial ("would have blocked" logging before enforcement),

with automatic rollback if the block rate jumps past the set limit.

Ratchet guard (F2.3): the baseline is computed from the runs BEFORE the
recent window, so a slowdown can never inflate the threshold meant to catch
it; the old threshold is read from the rule store (hard cap when none is
learned yet); movement per update and the hard cap are enforced here too.

The Investigator works from a multi-step workplan (see investigate()) and
consults episodic memory of past incidents, so proposals cite history instead
of reasoning from a single snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .memory import EpisodicMemory
from .thresholds import BaselineStore, FingerprintStats, RuleStore

THRESHOLD_HEADROOM = 1.5  # proposed threshold = baseline p95 x this
REGRESSION_RATIO = 1.5  # recent median above baseline median x this = regression


@dataclass
class ThresholdProposal:
    fingerprint: str
    metric: str  # "bytes_scanned" | "duration_sec"
    old_threshold: float
    new_threshold: float
    evidence: str
    status: str = "proposed"  # proposed -> shadow -> enforced | rolled_back | rejected | held
    # Aggregates the gates and the judge see (never SQL text).
    baseline_runs: int = 0
    baseline_p50: Optional[float] = None
    baseline_p95: Optional[float] = None
    baseline_p99: Optional[float] = None
    recent_median: Optional[float] = None
    baseline_max: Optional[float] = None
    history_span_days: Optional[float] = None
    baseline_trend_ratio: Optional[float] = None  # late-half median / early-half median
    would_block_rate_on_history: Optional[float] = None
    gate_violations: list[str] = field(default_factory=list)
    judge_reason: Optional[str] = None


@dataclass
class InvestigationReport:
    proposals: list[ThresholdProposal] = field(default_factory=list)
    tool_calls: int = 0  # eval metric: tool calls per investigation
    steps: list[str] = field(default_factory=list)  # workplan log


def propose_threshold(
    baseline: FingerprintStats,
    old_threshold: float,
    hard_cap: float,
    max_move: float = 0.25,
) -> Optional[float]:
    """Bounded tightening from a CLEAN baseline: target p95 x headroom, never
    above the hard cap, never more than max_move below the old threshold.
    None when the target would not tighten."""
    target = min(baseline.p95_secs * THRESHOLD_HEADROOM, hard_cap)
    if target >= old_threshold:
        return None
    return round(max(target, old_threshold * (1 - max_move)), 4)


def _trend(stats: FingerprintStats) -> Optional[float]:
    """Late-half median / early-half median of the baseline (drift signal)."""
    half = stats.run_count // 2
    early = FingerprintStats(stats.fingerprint, stats.runs[:half]).median_secs
    late = FingerprintStats(stats.fingerprint, stats.runs[half:]).median_secs
    return round(late / early, 3) if early and late else None


def investigate(
    store: BaselineStore,
    memory: EpisodicMemory,
    rules: Optional[RuleStore] = None,
    hard_cap_sec: float = 300.0,
    max_move: float = 0.25,
    recent_window: int = 5,
) -> InvestigationReport:
    """Multi-step investigation workplan:

    1. enumerate fingerprints with enough pre-window history (cold-start gate),
    2. regression check: recent window vs the shape's own earlier baseline,
    3. recall past incidents for context,
    4. draft a bounded threshold proposal with evidence.

    hard_cap_sec: the workload class's max_runtime_sec (customer.yaml).
    """
    rules = rules or RuleStore()
    report = InvestigationReport()
    report.steps.append(f"enumerated {len(store.stats)} fingerprints")
    for fp, stats in store.stats.items():
        baseline = FingerprintStats(fp, stats.runs[:-recent_window])
        window = stats.runs[-recent_window:]
        if baseline.run_count < store.min_runs_for_adaptive:
            report.steps.append(
                f"skip {fp[:40]}…: only {baseline.run_count} baseline runs (cold start)"
            )
            continue
        recent_med = sorted(s for _, s in window)[len(window) // 2]
        report.tool_calls += 1  # baseline lookup
        if not (baseline.median_secs and recent_med > baseline.median_secs * REGRESSION_RATIO):
            report.steps.append(f"no regression on {fp[:40]}…")
            continue
        incidents = memory.recall(fp)
        report.tool_calls += 1  # memory recall
        ctx = f" Past incident: {incidents[-1].summary}" if incidents else ""
        report.steps.append(
            f"regression on {fp[:40]}…: recent {recent_med:.2f}s "
            f"vs baseline {baseline.median_secs:.2f}s"
        )
        old = rules.threshold(fp, hard_cap_sec)
        report.tool_calls += 1  # rule store lookup
        new = propose_threshold(baseline, old, hard_cap_sec, max_move)
        if new is None:
            report.steps.append(f"threshold {old}s already at or below target — no proposal")
            continue
        report.proposals.append(
            ThresholdProposal(
                fingerprint=fp,
                metric="duration_sec",
                old_threshold=old,
                new_threshold=new,
                evidence=(
                    f"recent median {recent_med:.2f}s vs baseline median "
                    f"{baseline.median_secs:.2f}s (p95 {baseline.p95_secs:.2f}s) over "
                    f"{baseline.run_count} pre-regression runs; target "
                    f"{baseline.p95_secs * THRESHOLD_HEADROOM:.2f}s, bounded to "
                    f"{max_move:.0%} movement from {old}s.{ctx}"
                ),
                baseline_runs=baseline.run_count,
                baseline_p50=baseline.median_secs,
                baseline_p95=baseline.p95_secs,
                baseline_p99=baseline.p99_secs,
                baseline_max=baseline.max_secs,
                recent_median=recent_med,
                baseline_trend_ratio=_trend(baseline),
                would_block_rate_on_history=round(
                    sum(s > new for _, s in baseline.runs) / baseline.run_count, 4
                ),
            )
        )
    return report
