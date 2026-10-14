"""F4.2 watchdog, F4.3 tools + CLI, F3.1 rewrite verification with a fake
LLM, F3.2 judge payload privacy."""
import json

import pytest

from cost_guard.config import load_customer
from cost_guard.guard import decide
from cost_guard.interfaces import RewriteProposal
from cost_guard.investigator import ThresholdProposal
from cost_guard.llm import judge_payload
from cost_guard.runtime import read_log, run_query
from cost_guard.thresholds import RuleStore, ThresholdRule, ThresholdUpdater, fingerprint_query
from cost_guard.tracing import Tracer

SLOW = "SELECT SUM(range * 2) FROM range(5000000000)"


def _loose_customer(runtime_sec):
    cfg = load_customer()
    cfg["workload_classes"]["interactive"]["max_rows_processed"] = 10**12
    cfg["workload_classes"]["interactive"]["max_runtime_sec"] = runtime_sec
    return cfg


# ---- F4.2 -------------------------------------------------------------------

def test_watchdog_kills_past_hard_cap(synthetic_wh, tmp_path):
    out = run_query(SLOW, synthetic_wh, customer=_loose_customer(0.2),
                    log_path=tmp_path / "log.jsonl", tracer=Tracer())
    assert out.execution.status == "killed"
    assert out.execution.duration_sec < 2.0
    assert read_log(tmp_path / "log.jsonl")[0]["status"] == "killed"


def test_learned_threshold_is_the_timeout(synthetic_wh, tmp_path):
    fp = fingerprint_query(SLOW)
    up = ThresholdUpdater(hard_caps={"max_runtime_sec": 300},
                          rules=RuleStore({fp: ThresholdRule(fp, "duration_sec", enforced=0.2)}))
    out = run_query(SLOW, synthetic_wh, customer=_loose_customer(300), updater=up,
                    log_path=tmp_path / "log.jsonl", tracer=Tracer())
    assert out.timeout_sec == 0.2
    assert out.execution.status == "killed"


def test_blocked_query_never_executes(synthetic_wh, tmp_path):
    out = run_query("SELECT * FROM users, orders WHERE users.email = 'alice@example.com'",
                    synthetic_wh, log_path=tmp_path / "log.jsonl", tracer=Tracer())
    assert out.verdict.decision == "block" and out.execution is None
    rec = read_log(tmp_path / "log.jsonl")[0]
    assert rec["status"] == "blocked"
    # The log keeps the literal-free fingerprint, never raw SQL or literals.
    assert "alice" not in json.dumps(rec)
    assert set(rec) >= {"fingerprint", "duration_sec", "timeout_sec"}


# ---- F4.3 -------------------------------------------------------------------

def test_tools(synthetic_wh, tmp_path):
    from cost_guard.tools import get_query_history, get_table_metadata, run_sample_query

    md = get_table_metadata("orders", backend=synthetic_wh)
    assert md.num_rows == 1_000_000 and md.partitioned_by == "created_at"
    q = "SELECT status, COUNT(*) FROM orders GROUP BY 1"
    assert run_sample_query(q, backend=synthetic_wh) == run_sample_query(q, backend=synthetic_wh)
    with pytest.raises(PermissionError):
        run_sample_query("DELETE FROM orders", backend=synthetic_wh)
    log = tmp_path / "log.jsonl"
    q = "SELECT id FROM users WHERE id = 3"
    run_query(q, synthetic_wh, log_path=log, tracer=Tracer())
    hist = get_query_history(fingerprint_query(q), log_path=log)
    assert len(hist) == 1 and hist[0]["status"] == "completed"


def test_cli_check_and_investigate(synthetic_wh, tmp_path, capsys):
    from cost_guard.__main__ import main

    assert main(["--db", synthetic_wh.db_path, "check", "SELECT id FROM users WHERE id = 1"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "allow"
    assert main(["--db", synthetic_wh.db_path, "check", "SELECT * FROM users, orders"]) == 2
    capsys.readouterr()
    assert main(["investigate", "--log", str(tmp_path / "none.jsonl")]) == 0


# ---- F3.1 / F3.3 with a fake LLM -------------------------------------------

class FakeLLM:
    def __init__(self, rewrite):
        self.rewrite = rewrite
        self.explained = 0

    def propose_rewrite(self, sql, findings):
        return RewriteProposal(sql, self.rewrite, "fake") if self.rewrite else None

    def explain_flag(self, sql, findings):
        self.explained += 1
        return "plain-language explanation"


WRAPPED = ("SELECT order_id, status FROM orders "
           "WHERE DATE_TRUNC('month', created_at) = DATE '2024-01-01'")


def test_correct_llm_rewrite_is_offered_after_verification(synthetic_wh):
    good = ("SELECT order_id, status FROM orders "
            "WHERE created_at >= DATE '2024-01-01' AND created_at < DATE '2024-02-01'")
    v = decide(WRAPPED, backend=synthetic_wh, tracer=Tracer(), llm=FakeLLM(good))
    assert v.decision == "rewrite" and v.rewrite_source == "llm"
    assert "match" in v.verification


def test_wrong_llm_rewrite_is_rejected(synthetic_wh):
    wrong = ("SELECT order_id, status FROM orders "
             "WHERE created_at >= DATE '2024-01-01' AND created_at < DATE '2024-01-15'")
    llm = FakeLLM(wrong)
    v = decide(WRAPPED, backend=synthetic_wh, tracer=Tracer(), llm=llm)
    assert v.decision == "warn"
    assert any("llm rewrite failed verification" in f for f in v.findings)
    assert v.explanation == "plain-language explanation"  # F3.3: warn gets explained


def test_llm_not_called_on_clean_queries(synthetic_wh):
    llm = FakeLLM("SELECT 1")
    v = decide("SELECT id FROM users WHERE id = 1", backend=synthetic_wh, tracer=Tracer(), llm=llm)
    assert v.decision == "allow" and llm.explained == 0 and v.explanation is None


def test_llm_rewrite_that_writes_is_refused(synthetic_wh):
    v = decide(WRAPPED, backend=synthetic_wh, tracer=Tracer(), llm=FakeLLM("DROP TABLE orders"))
    assert v.decision == "warn"
    assert any("not read-only" in f for f in v.findings)


# ---- F3.2 -------------------------------------------------------------------

def test_judge_sees_aggregates_only():
    fp = fingerprint_query("SELECT secret_column FROM payroll WHERE id = 1")
    p = ThresholdProposal(fp, "duration_sec", 1.0, 0.8, "evidence with SQL shape " + fp,
                          baseline_runs=40, baseline_p50=0.3, baseline_p95=0.4)
    payload = judge_payload(p)
    assert "payroll" not in payload and "secret_column" not in payload
    assert "evidence" not in payload
    assert "baseline_p95_sec: 0.4" in payload
