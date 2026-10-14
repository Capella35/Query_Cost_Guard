"""Layer 2 adaptive thresholds: fingerprinting, baselines, updater.

- Fingerprint queries by normalizing literals.
- Per-fingerprint baselines: median and p95 of bytes scanned AND duration.
- Anomaly rule: warn when a query is far above its own history
  (above p95 or a few times the median). Regression rule for runtime.
- Cold start: static rules only until a fingerprint has ~20 runs.
- Runtime is noisy (caching, concurrency): percentiles with a minimum run
  count; report relative numbers in the DuckDB demo.

Ratchet guards (history full of bad queries must not teach "expensive is normal"):
- absolute hard caps never adapt; learned thresholds sit below them,
- bounded movement per update,
- tighten only on evidence (e.g. a verified-equivalent cheaper rewrite exists),
- shadow mode first: log "would have blocked" before enforcing,
- auto-rollback if a new threshold's block rate jumps past the set limit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_NUMBER_LITERAL = re.compile(r"\b\d+(?:\.\d+)?\b")
_WS = re.compile(r"\s+")
_OP_WS = re.compile(r"\s*([=,()<>!+\-*/])\s*")


def fingerprint_query(sql: str) -> str:
    """Normalize a query into a shape fingerprint: lowercase, literals replaced,
    whitespace collapsed (including around operators). Same shape + different
    constants -> same fingerprint."""
    if not sql:
        return ""
    normalized = sql.strip().lower()
    normalized = _STRING_LITERAL.sub("'?'", normalized)
    normalized = _NUMBER_LITERAL.sub("?", normalized)
    normalized = _OP_WS.sub(r"\1", normalized)
    return _WS.sub(" ", normalized).strip()


@dataclass
class FingerprintStats:
    fingerprint: str
    runs: list = field(default_factory=list)  # [(bytes_scanned, duration_sec)]

    @property
    def run_count(self) -> int:
        return len(self.runs)

    def _pct(self, idx: int, q: float):
        vals = sorted(r[idx] for r in self.runs)
        if not vals:
            return None
        if len(vals) == 1:
            return float(vals[0])
        k = min(int(q * len(vals)), len(vals) - 1)
        return float(vals[k])

    @property
    def median_secs(self):
        return self._pct(1, 0.5)

    @property
    def p95_secs(self):
        return self._pct(1, 0.95)

    @property
    def p99_secs(self):
        return self._pct(1, 0.99)

    @property
    def max_secs(self):
        return max((r[1] for r in self.runs), default=None)

    @property
    def median_bytes(self):
        return self._pct(0, 0.5)

    @property
    def p95_bytes(self):
        return self._pct(0, 0.95)


@dataclass
class DriftAwareBaseline:
    """Expected runtime for a query shape that may drift (e.g. a table that
    grows): a robust trend on log(runtime) over time, plus how far above the
    trend this shape's runs normally land.

    A run is anomalous when it beats its trend-expected runtime by more than
    both (a) the history's own `residual_q` residual quantile — the shape's
    normal spread, recurring peaks included — and (b) `min_ratio`.
    residual_q is 0.99 (≈ the largest past residual at 20–100 runs) because a
    legitimate recurring peak (a 10x month-end run, ~10% of runs) has its own
    spread: at 0.95 the threshold sat on the lowest peak and flagged the next
    one. Found on the hard replay's development seed, so it is a tuning
    choice; the held-out seeds are the check.

    Ratchet guard: the trend is used only when the first and second half of
    the history agree on its direction and the halves' slopes are within
    `max_slope_ratio` of each other; otherwise the slope is 0 (flat median).
    A regression that started inside the history then shows up as a broken
    trend, not as "slower is normal". Hard caps still bound everything.
    """

    intercept: float  # log-seconds at t = 0
    slope: float  # log-seconds per hour (0 when the trend was not trusted)
    residual_upper: float  # residual_q quantile of log(observed / expected)
    runs: int
    trend_trusted: bool

    @staticmethod
    def _theil_sen(ts: list[float], ys: list[float]) -> float:
        slopes = sorted((ys[j] - ys[i]) / (ts[j] - ts[i])
                        for i in range(len(ts)) for j in range(i + 1, len(ts)) if ts[j] != ts[i])
        return slopes[len(slopes) // 2] if slopes else 0.0

    @classmethod
    def fit(cls, times_h: list[float], durations: list[float], residual_q: float = 0.99,
            max_slope_ratio: float = 3.0) -> "DriftAwareBaseline":
        import math

        pts = sorted((t, math.log(d)) for t, d in zip(times_h, durations) if d and d > 0)
        ts, ys = [p[0] for p in pts], [p[1] for p in pts]
        slope = cls._theil_sen(ts, ys)
        half = len(ts) // 2
        s1, s2 = cls._theil_sen(ts[:half], ys[:half]), cls._theil_sen(ts[half:], ys[half:])
        trusted = (len(ts) >= 8 and s1 * s2 > 0
                   and max(abs(s1), abs(s2)) <= max_slope_ratio * min(abs(s1), abs(s2)))
        if not trusted:
            slope = 0.0
        intercept = sorted(y - slope * t for t, y in zip(ts, ys))[len(ts) // 2]
        resid = sorted(y - (intercept + slope * t) for t, y in zip(ts, ys))
        k = min(int(residual_q * len(resid)), len(resid) - 1)
        return cls(intercept, slope, resid[k] if resid else 0.0, len(ts), trusted)

    def expected(self, t_h: float) -> float:
        import math

        return math.exp(self.intercept + self.slope * t_h)

    def excess(self, t_h: float, duration: float) -> float:
        """observed / expected runtime."""
        return duration / self.expected(t_h)

    def is_anomaly(self, t_h: float, duration: float, min_ratio: float) -> bool:
        import math

        r = math.log(duration) - math.log(self.expected(t_h))
        return r > max(self.residual_upper, math.log(min_ratio))


@dataclass
class BaselineStore:
    """Per-fingerprint baselines computed from the query log."""

    stats: dict[str, FingerprintStats] = field(default_factory=dict)
    min_runs_for_adaptive: int = 20  # cold start: static rules only below this

    def record(self, sql: str, bytes_scanned: int, duration_sec: float) -> None:
        fp = fingerprint_query(sql)
        self.stats.setdefault(fp, FingerprintStats(fp)).runs.append(
            (bytes_scanned, duration_sec)
        )

    def baseline(self, sql: str) -> FingerprintStats | None:
        return self.stats.get(fingerprint_query(sql))


@dataclass
class ThresholdRule:
    """A learned per-fingerprint threshold and its lifecycle state."""

    fingerprint: str
    metric: str  # "duration_sec"
    enforced: float  # the threshold in force
    shadow: float | None = None  # candidate under trial ("would have blocked" only)
    previous: float | None = None  # value to roll back to after a promotion
    status: str = "enforced"  # "enforced" | "shadow" | "probation"
    seen: int = 0  # runs observed in the current shadow/probation window
    hits: int = 0  # of those, runs the candidate/new threshold would block


@dataclass
class RuleStore:
    """Learned thresholds per fingerprint. Absent = the hard cap applies."""

    rules: dict[str, ThresholdRule] = field(default_factory=dict)

    def threshold(self, fingerprint: str, default: float) -> float:
        r = self.rules.get(fingerprint)
        return min(r.enforced, default) if r else default

    def save(self, path) -> None:
        import json
        from dataclasses import asdict
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({k: asdict(v) for k, v in self.rules.items()}, indent=2))

    @classmethod
    def load(cls, path) -> "RuleStore":
        import json
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            return cls()
        return cls({k: ThresholdRule(**v) for k, v in json.loads(p.read_text()).items()})


def gate_violations(
    proposal,
    hard_cap: float,
    max_move: float = 0.25,
    min_runs: int = 20,
) -> list[str]:
    """Deterministic safety gates every threshold proposal must pass before
    anything else (judge, shadow) sees it. Empty list = passes.

    - never above the absolute hard cap,
    - tighten only (a loosening proposal is how "expensive becomes normal"),
    - bounded movement per update,
    - enough baseline runs to trust the percentiles,
    - not below the baseline p95 (it would block normal traffic).
    """
    v = []
    new, old = proposal.new_threshold, proposal.old_threshold
    if new > hard_cap:
        v.append(f"above hard cap ({new} > {hard_cap})")
    if new >= old:
        v.append(f"does not tighten ({new} >= {old})")
    elif old > 0 and (old - new) / old > max_move + 1e-9:
        v.append(f"moves {(old - new) / old:.0%} > {max_move:.0%} per update")
    if proposal.baseline_runs < min_runs:
        v.append(f"only {proposal.baseline_runs} baseline runs (< {min_runs})")
    if proposal.baseline_p95 is not None and new < proposal.baseline_p95:
        v.append(f"below baseline p95 ({new} < {proposal.baseline_p95}) — blocks normal runs")
    return v


@dataclass
class ThresholdUpdater:
    """Applies Investigator proposals: gates -> (judge) -> shadow -> enforce,
    with auto-rollback.

    Lifecycle per fingerprint:
    1. apply(): deterministic gates, then the optional judge (may reject or
       hold, never enforce). A passing proposal enters SHADOW: the old
       threshold stays in force; the candidate only records "would have
       blocked".
    2. observe(): after `trial_runs` shadow observations, a candidate whose
       would-block rate exceeds rollback_block_rate is rolled back (never
       enforced); otherwise it is ENFORCED and enters probation.
    3. During probation, if the real block rate over `trial_runs` runs jumps
       past rollback_block_rate, the previous threshold is restored.
    """

    hard_caps: dict = field(default_factory=dict)  # workload class caps from customer.yaml; never adapt
    rules: RuleStore = field(default_factory=RuleStore)
    max_move_per_update: float = 0.25  # threshold may move at most 25% per update
    rollback_block_rate: float = 0.05  # rollback if block rate jumps past 5%
    trial_runs: int = 20  # shadow and probation window length
    min_runs: int = 20
    judge: object = None  # optional: .judge(proposal) -> "approve" | "reject" | "hold"
    events: list = field(default_factory=list)  # (fingerprint, event) audit log

    @property
    def hard_cap(self) -> float:
        return float(self.hard_caps.get("max_runtime_sec", float("inf")))

    def current(self, fingerprint: str) -> float:
        return self.rules.threshold(fingerprint, self.hard_cap)

    def apply(self, proposal) -> str:
        """proposed -> shadow (or rejected / held). Returns the new status."""
        violations = gate_violations(
            proposal, self.hard_cap, self.max_move_per_update, self.min_runs
        )
        rule = self.rules.rules.get(proposal.fingerprint)
        if rule is not None and rule.status != "enforced":
            violations.append(f"a trial is already running ({rule.status})")
        if violations:
            proposal.status = "rejected"
            proposal.gate_violations = violations
            self.events.append((proposal.fingerprint, "rejected: " + "; ".join(violations)))
            return proposal.status
        if self.judge is not None:
            verdict = self.judge.judge(proposal)
            if verdict != "approve":
                proposal.status = "held" if verdict == "hold" else "rejected"
                self.events.append((proposal.fingerprint, f"judge: {verdict}"))
                return proposal.status
        if rule is None:
            rule = ThresholdRule(proposal.fingerprint, proposal.metric, self.current(proposal.fingerprint))
            self.rules.rules[proposal.fingerprint] = rule
        rule.shadow, rule.status, rule.seen, rule.hits = proposal.new_threshold, "shadow", 0, 0
        proposal.status = "shadow"
        self.events.append((proposal.fingerprint, f"shadow trial at {proposal.new_threshold}"))
        return proposal.status

    def observe(self, fingerprint: str, duration_sec: float) -> str | None:
        """Feed one completed (or killed) run. Returns an event name when the
        rule changes state: "enforced" or "rolled_back"."""
        rule = self.rules.rules.get(fingerprint)
        if rule is None or rule.status == "enforced":
            return None
        limit = rule.shadow if rule.status == "shadow" else rule.enforced
        rule.seen += 1
        rule.hits += duration_sec > limit
        if rule.seen < self.trial_runs:
            return None
        rate = rule.hits / rule.seen
        rule.seen = rule.hits = 0
        if rule.status == "shadow":
            if rate > self.rollback_block_rate:
                rule.shadow, rule.status = None, "enforced"
                event = f"rolled_back: shadow would-block rate {rate:.0%}"
            else:
                rule.previous, rule.enforced = rule.enforced, rule.shadow
                rule.shadow, rule.status = None, "probation"
                event = f"enforced at {rule.enforced}"
        else:  # probation
            if rate > self.rollback_block_rate:
                rule.enforced, rule.previous, rule.status = rule.previous, None, "enforced"
                event = f"rolled_back: block rate {rate:.0%} after enforcement"
            else:
                rule.previous, rule.status = None, "enforced"
                event = "probation passed"
        self.events.append((fingerprint, event))
        return event.split(":")[0].split(" ")[0]
