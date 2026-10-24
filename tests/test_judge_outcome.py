"""The threshold judge records how each call ended and what it cost, so a
refusal or an error (both hold) is never read as a judgment."""
from types import SimpleNamespace

from query_preflight.interfaces import NullLLM
from query_preflight.investigator import ThresholdProposal
from query_preflight.llm import JudgmentOut, ClaudeLLM


def _prop():
    return ThresholdProposal(fingerprint="fp", metric="duration_sec", old_threshold=1.0,
                             new_threshold=0.8, evidence="", baseline_runs=40)


def _llm(parse):
    return ClaudeLLM(client=SimpleNamespace(beta=SimpleNamespace(
        messages=SimpleNamespace(parse=parse))))


USAGE = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0)


def test_judged_records_reason_and_cost():
    llm = _llm(lambda **kw: SimpleNamespace(
        stop_reason="end_turn", usage=USAGE,
        parsed_output=JudgmentOut(decision="reject", reason="trend ratio 2.1: data growth")))
    p = _prop()
    assert llm.judge(p) == "reject"
    assert p.judge_outcome == "judged" and "trend ratio" in p.judge_reason
    assert abs(p.judge_usd - (1000 * 0.10 + 200 * 0.50) / 1e6) < 1e-12


def test_refusal_holds_and_says_so():
    llm = _llm(lambda **kw: SimpleNamespace(stop_reason="refusal", usage=USAGE, parsed_output=None))
    p = _prop()
    assert llm.judge(p) == "hold"
    assert p.judge_outcome == "refused" and p.judge_reason is None and p.judge_usd > 0


def test_empty_output_names_the_stop_reason():
    llm = _llm(lambda **kw: SimpleNamespace(stop_reason="max_tokens", usage=None, parsed_output=None))
    p = _prop()
    assert llm.judge(p) == "hold" and p.judge_outcome == "no output (max_tokens)"
    assert p.judge_usd is None


def test_api_error_holds_instead_of_crashing():
    def boom(**kw):
        raise ConnectionError("network down")
    p = _prop()
    assert _llm(boom).judge(p) == "hold"
    assert p.judge_outcome == "error: ConnectionError" and p.judge_usd is None


def test_no_llm_holds_as_no_judge():
    p = _prop()
    assert NullLLM().judge(p) == "hold" and p.judge_outcome == "no judge"


def test_eval_tells_a_lazy_judge_from_a_safe_one(tmp_path, monkeypatch):
    """Holding on every subtle row is safe (6/6) but not understanding (1/6:
    only S3 accepts a hold)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
    import run_judge_eval
    import query_preflight.llm as L

    calls = {"n": 0}

    def parse(**kw):  # approves the 8 good rows, holds on the 6 subtle ones
        calls["n"] += 1
        return SimpleNamespace(stop_reason="end_turn", usage=None, parsed_output=JudgmentOut(
            decision="approve" if calls["n"] <= 8 else "hold", reason="r"))

    monkeypatch.setattr(L, "ClaudeLLM", lambda: ClaudeLLM(client=SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)))))
    monkeypatch.setattr(run_judge_eval, "ROOT", tmp_path)
    (tmp_path / "eval").mkdir()
    monkeypatch.setattr(sys, "argv", ["run_judge_eval.py", "--llm", "claude"])
    run_judge_eval.main()
    md = (tmp_path / "eval" / "RESULTS_judge.md").read_text()
    assert "| 1_bad_wrongly_approved | 0/6 (must be 0) |" in md
    assert "| 2_subtle_bad_rejected_or_held | 6/6 |" in md
    assert "| 2b_subtle_bad_right_verdict | 1/6 (reject; S3 reject or hold) |" in md


def test_prompt_states_the_slowdown_that_triggers_every_proposal():
    """Option 1 (owner, 2026-10-24): the judge is told a proposal follows a
    slowdown it is meant to stop; the ratio it cites is the Investigator's."""
    from query_preflight.investigator import REGRESSION_RATIO
    from query_preflight.llm import JUDGE_SYSTEM

    assert f"at least {REGRESSION_RATIO:g}x its usual median" in JUDGE_SYSTEM
    assert "intended effect, not a reason to reject" in JUDGE_SYSTEM


def _judge_eval(tmp_path, monkeypatch, argv, first_run_text=None):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
    import run_judge_eval
    import query_preflight.llm as L

    monkeypatch.setattr(L, "ClaudeLLM", lambda: ClaudeLLM(client=SimpleNamespace(beta=SimpleNamespace(
        messages=SimpleNamespace(parse=lambda **kw: SimpleNamespace(
            stop_reason="end_turn", usage=None,
            parsed_output=JudgmentOut(decision="reject", reason="r")))))))
    monkeypatch.setattr(run_judge_eval, "ROOT", tmp_path)
    (tmp_path / "eval").mkdir(exist_ok=True)
    if first_run_text is not None:
        (tmp_path / "eval" / "RESULTS_judge.md").write_text(first_run_text)
    monkeypatch.setattr(sys, "argv", ["run_judge_eval.py", *argv])
    run_judge_eval.main()


def test_the_first_live_run_is_never_overwritten(tmp_path, monkeypatch):
    import pytest
    first = "Generated ..., judge: `claude`, ...\nthe first run\n"
    with pytest.raises(SystemExit, match="--rerun"):
        _judge_eval(tmp_path, monkeypatch, ["--llm", "claude"], first)
    _judge_eval(tmp_path, monkeypatch, [], first)  # gates only: prints, writes nothing
    _judge_eval(tmp_path, monkeypatch, ["--llm", "claude", "--rerun"], first)
    assert (tmp_path / "eval" / "RESULTS_judge.md").read_text() == first
    assert "**RERUN**" in (tmp_path / "eval" / "RESULTS_judge_rerun.md").read_text()
