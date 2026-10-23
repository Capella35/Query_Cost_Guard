"""Tests for episodic memory and the Investigator workplan."""

from query_preflight.investigator import investigate
from query_preflight.memory import EpisodicMemory, Incident
from query_preflight.thresholds import BaselineStore, fingerprint_query


def test_memory_roundtrip(tmp_path):
    mem = EpisodicMemory(tmp_path / "inc.jsonl")
    assert mem.recall("abc") == []
    mem.record(
        Incident(
            fingerprint="abc",
            summary="slow on 2026-10-01",
            outcome="threshold tightened",
        )
    )
    got = mem.recall("abc")
    assert len(got) == 1
    assert got[0].outcome == "threshold tightened"
    assert mem.recall("other") == []


def test_investigate_finds_regression_with_memory_context(tmp_path):
    store = BaselineStore(min_runs_for_adaptive=20)
    q = "SELECT name FROM users WHERE id = 1"
    for _ in range(25):
        store.record(q, 50_000, 0.3)
    for _ in range(5):  # regression in the recent window
        store.record(q, 50_000, 2.0)
    mem = EpisodicMemory(tmp_path / "inc.jsonl")
    mem.record(
        Incident(
            fingerprint=fingerprint_query(q),
            summary="past slowdown, same shape",
            outcome="warned",
        )
    )
    rep = investigate(store, mem)
    assert len(rep.proposals) == 1
    assert "past slowdown" in rep.proposals[0].evidence
    assert rep.steps  # workplan log is non-empty
    assert rep.tool_calls >= 2  # baseline lookup + memory recall


def test_investigate_skips_cold_fingerprints(tmp_path):
    store = BaselineStore(min_runs_for_adaptive=20)
    store.record("SELECT 1", 10, 0.01)
    rep = investigate(store, EpisodicMemory(tmp_path / "x.jsonl"))
    assert rep.proposals == []
    assert any("cold start" in s for s in rep.steps)


def test_investigate_no_regression_no_proposal(tmp_path):
    store = BaselineStore(min_runs_for_adaptive=20)
    q = "SELECT name FROM users WHERE id = 2"
    for _ in range(25):
        store.record(q, 50_000, 0.3)
    rep = investigate(store, EpisodicMemory(tmp_path / "y.jsonl"))
    assert rep.proposals == []
