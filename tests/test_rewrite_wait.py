"""Rewrite verification with a time limit and a memory (guard._verify): past
the limit the check answers without the rewrite, verification finishes in the
background, and the same query checked again gets the verified rewrite at
once; failed verifications are remembered too (no repeated cost)."""
import threading
import time

import pytest

import importlib
from cost_guard.guard import decide
from cost_guard.tracing import Tracer

# the module (cost_guard.tools re-exports the function under the same name)
cr = importlib.import_module("cost_guard.tools.compare_results")

WRAPPED = ("SELECT order_id, status FROM orders "
           "WHERE DATE_TRUNC('month', created_at) = DATE '2024-01-01'")


@pytest.fixture()
def slow_verify(monkeypatch):
    """Verification takes `delay` seconds; counts calls; can be gated."""
    real = cr.compare_results
    state = {"calls": 0, "delay": 0.6, "fail": False}

    def slow(*a, **k):
        state["calls"] += 1
        time.sleep(state["delay"])
        res = real(*a, **k)
        if state["fail"]:
            res.passed, res.detail = False, "forced failure"
        return res

    monkeypatch.setattr(cr, "compare_results", slow)
    return state


def test_default_waits_for_verification(synthetic_wh, slow_verify):
    v = decide(WRAPPED, backend=synthetic_wh, tracer=Tracer())  # rewrite_wait_sec=None
    assert v.decision == "rewrite" and slow_verify["calls"] == 1


def test_time_limit_then_remembered_rewrite(synthetic_wh, slow_verify):
    t0 = time.perf_counter()
    first = decide(WRAPPED, backend=synthetic_wh, tracer=Tracer(), rewrite_wait_sec=0.1)
    assert time.perf_counter() - t0 < 0.5  # did not wait for the 0.6 s verification
    assert first.decision == "warn" and first.rewritten_sql is None
    assert any("still being verified in the background" in f for f in first.findings)
    time.sleep(0.8)  # background verification finishes
    t0 = time.perf_counter()
    second = decide(WRAPPED, backend=synthetic_wh, tracer=Tracer(), rewrite_wait_sec=0.1)
    assert second.decision == "rewrite" and "remembered" in second.verification
    assert time.perf_counter() - t0 < 0.5 and slow_verify["calls"] == 1  # no second verification


def test_concurrent_checks_share_one_verification(synthetic_wh, slow_verify):
    out = []
    threads = [threading.Thread(target=lambda: out.append(
        decide(WRAPPED, backend=synthetic_wh, tracer=Tracer(), rewrite_wait_sec=5).decision))
        for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert out == ["rewrite"] * 4 and slow_verify["calls"] == 1


def test_failed_verification_is_remembered(synthetic_wh, slow_verify):
    slow_verify.update(delay=0.0, fail=True)
    for _ in range(3):
        v = decide(WRAPPED, backend=synthetic_wh, tracer=Tracer(), rewrite_wait_sec=2)
        assert v.decision == "warn" and v.rewritten_sql is None
    assert slow_verify["calls"] == 1
    assert any("failed verification" in f and "(remembered)" in f for f in v.findings)
