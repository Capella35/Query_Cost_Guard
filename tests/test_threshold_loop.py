"""F2.3 ratchet fix and F4.1 updater lifecycle (gates, shadow, rollback)."""
import random

from query_preflight.investigator import ThresholdProposal, investigate
from query_preflight.memory import EpisodicMemory
from query_preflight.thresholds import (
    BaselineStore,
    RuleStore,
    ThresholdRule,
    ThresholdUpdater,
    fingerprint_query,
    gate_violations,
)

Q = "SELECT name FROM users WHERE id = 7"
FP = fingerprint_query(Q)


def _store(recent_secs: float, n_base=25, n_recent=5) -> BaselineStore:
    rng = random.Random(1)
    store = BaselineStore(min_runs_for_adaptive=20)
    for _ in range(n_base):
        store.record(Q, 50_000, 0.3 + rng.uniform(-0.02, 0.02))
    for _ in range(n_recent):
        store.record(Q, 50_000, recent_secs)
    return store


def _rules(old: float) -> RuleStore:
    return RuleStore({FP: ThresholdRule(FP, "duration_sec", enforced=old)})


# ---- F2.3 -------------------------------------------------------------------

def test_recent_slow_runs_do_not_raise_the_threshold(tmp_path):
    mem = EpisodicMemory(tmp_path / "m.jsonl")
    # Old code: p95 over ALL runs (incl. the 2.0s regression) x1.5 = 3.0s,
    # which LOOSENS a 1.0s threshold. The baseline must exclude the window.
    rep = investigate(_store(2.0), mem, rules=_rules(1.0), hard_cap_sec=300)
    assert len(rep.proposals) == 1
    p = rep.proposals[0]
    assert p.old_threshold == 1.0  # read from the rule store, not hard-coded
    assert p.new_threshold <= p.old_threshold
    # A worse regression must not move the proposal at all.
    worse = investigate(_store(20.0), mem, rules=_rules(1.0), hard_cap_sec=300).proposals[0]
    assert worse.new_threshold == p.new_threshold


def test_bounded_tightening_passes_gates(tmp_path):
    rep = investigate(_store(2.0), EpisodicMemory(tmp_path / "m.jsonl"), hard_cap_sec=300)
    p = rep.proposals[0]
    assert p.old_threshold == 300  # nothing learned yet -> hard cap
    assert p.new_threshold == 225  # 25% max movement, not a jump to ~0.45s
    assert gate_violations(p, hard_cap=300) == []


def test_no_proposal_when_already_tight(tmp_path):
    rep = investigate(_store(2.0), EpisodicMemory(tmp_path / "m.jsonl"),
                      rules=_rules(0.4), hard_cap_sec=300)
    assert rep.proposals == []
    assert any("already at or below target" in s for s in rep.steps)


# ---- F3.2 gates: every bad-proposal type is caught deterministically --------

def _prop(old, new, runs=40, p95=0.4):
    return ThresholdProposal(FP, "duration_sec", old, new, "", baseline_runs=runs,
                             baseline_p50=0.3, baseline_p95=p95)


def test_gates_catch_each_bad_type():
    assert gate_violations(_prop(1.0, 0.9), hard_cap=300) == []
    assert any("does not tighten" in v for v in gate_violations(_prop(1.0, 1.4), 300))  # loosened to p99
    assert any("baseline runs" in v for v in gate_violations(_prop(1.0, 0.9, runs=5), 300))
    assert any("above hard cap" in v for v in gate_violations(_prop(500, 400), 300))
    assert any("moves" in v for v in gate_violations(_prop(1.0, 0.5), 300))
    assert any("below baseline p95" in v for v in gate_violations(_prop(0.5, 0.39), 300))


# ---- F4.1 -------------------------------------------------------------------

def _updater(**kw):
    return ThresholdUpdater(hard_caps={"max_runtime_sec": 300}, rules=_rules(1.0),
                            trial_runs=20, **kw)


def test_shadow_then_enforce_then_probation_passes():
    up = _updater()
    assert up.apply(_prop(1.0, 0.8)) == "shadow"
    assert up.current(FP) == 1.0  # shadow never enforces
    events = [up.observe(FP, 0.3) for _ in range(20)]
    assert events[-1] == "enforced"
    assert up.current(FP) == 0.8
    events = [up.observe(FP, 0.3) for _ in range(20)]
    assert events[-1] == "probation"
    assert up.rules.rules[FP].status == "enforced"


def test_bad_threshold_rolls_back_in_shadow():
    """Offline replay: a candidate that would block >5% of real traffic is
    never enforced."""
    up = _updater()
    up.apply(_prop(1.0, 0.8))
    for i in range(20):
        up.observe(FP, 0.95 if i % 5 == 0 else 0.3)  # 20% of runs above 0.8
    assert up.rules.rules[FP].status == "enforced"
    assert up.current(FP) == 1.0
    assert "rolled_back" in up.events[-1][1]


def test_enforced_threshold_rolls_back_when_block_rate_jumps():
    up = _updater()
    up.apply(_prop(1.0, 0.8))
    for _ in range(20):
        up.observe(FP, 0.3)
    assert up.current(FP) == 0.8
    for i in range(20):  # traffic shifts after enforcement
        up.observe(FP, 0.9 if i % 4 == 0 else 0.3)
    assert up.current(FP) == 1.0  # previous threshold restored
    assert "after enforcement" in up.events[-1][1]


def test_gate_failure_and_judge_hold_never_shadow():
    up = _updater()
    assert up.apply(_prop(1.0, 1.4)) == "rejected"

    class HoldJudge:
        def judge(self, p):
            return "hold"

    up = _updater(judge=HoldJudge())
    assert up.apply(_prop(1.0, 0.8)) == "held"
    assert up.rules.rules[FP].status == "enforced"


def test_rule_store_roundtrip(tmp_path):
    rs = _rules(1.0)
    rs.save(tmp_path / "rules.json")
    assert RuleStore.load(tmp_path / "rules.json").threshold(FP, 300) == 1.0
    assert RuleStore.load(tmp_path / "missing.json").threshold(FP, 300) == 300
