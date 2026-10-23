"""thresholds.DriftAwareBaseline: drift is expected, a new regression is not,
a recurring peak is normal, and a broken trend is not extrapolated."""
import math
import random

from query_preflight.thresholds import DriftAwareBaseline

HOURS = [12.0 * i for i in range(42)]


def _fit(durs):
    return DriftAwareBaseline.fit(HOURS, durs)


def test_steady_growth_is_expected_not_anomalous():
    r = random.Random(1)
    durs = [0.2 * 1.015**i * r.uniform(0.95, 1.05) for i in range(42)]
    b = _fit(durs)
    assert b.trend_trusted and b.slope > 0
    for i in range(42, 60):  # growth continues past the history
        assert not b.is_anomaly(12.0 * i, 0.2 * 1.015**i, min_ratio=1.5)


def test_regression_after_flat_history_is_anomalous():
    r = random.Random(2)
    b = _fit([0.25 * r.uniform(0.95, 1.05) for _ in range(42)])
    assert b.is_anomaly(12.0 * 50, 0.25 * 1.8, min_ratio=1.5)
    assert not b.is_anomaly(12.0 * 50, 0.25 * 1.2, min_ratio=1.5)  # below min_ratio


def test_recurring_peak_is_normal():
    r = random.Random(3)
    durs = [(3.0 if i % 10 == 9 else 0.3) * r.uniform(0.9, 1.1) for i in range(42)]
    b = _fit(durs)
    assert not b.is_anomaly(12.0 * 49, 3.0 * 1.1, min_ratio=1.5)  # month-end peak
    assert b.is_anomaly(12.0 * 50, 0.3 * 40, min_ratio=1.5)  # far beyond anything seen


def test_broken_trend_is_not_extrapolated():
    # Flat first half, steep rise in the second: the halves disagree, so the
    # rise is not learned as normal growth (ratchet guard).
    durs = [0.25] * 21 + [0.25 * 1.08**i for i in range(1, 22)]
    b = _fit(durs)
    assert not b.trend_trusted and b.slope == 0.0
    assert b.is_anomaly(12.0 * 50, 0.25 * 1.08**30, min_ratio=1.5)


def test_expected_follows_trend():
    durs = [math.exp(-1.0 + 0.001 * t) for t in HOURS]
    b = _fit(durs)
    assert abs(b.expected(600.0) - math.exp(-1.0 + 0.6)) < 1e-6
