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


# A flagged pattern the deterministic catalog does not fix (HAVING without an
# aggregate), so the LLM proposer is consulted.
HAVING = ("SELECT status, COUNT(*) AS n FROM orders WHERE created_at >= '2024-01-01' "
          "GROUP BY status HAVING status <> 'cancelled'")
WRAPPED = ("SELECT order_id, status FROM orders "
           "WHERE DATE_TRUNC('month', created_at) = DATE '2024-01-01'")


def test_correct_llm_rewrite_is_offered_after_verification(synthetic_wh):
    good = ("SELECT status, COUNT(*) AS n FROM orders WHERE created_at >= '2024-01-01' "
            "AND status <> 'cancelled' GROUP BY status")
    v = decide(HAVING, backend=synthetic_wh, tracer=Tracer(), llm=FakeLLM(good))
    assert v.decision == "rewrite" and v.rewrite_source == "llm"
    assert "match" in v.verification


def test_wrong_llm_rewrite_is_rejected(synthetic_wh):
    wrong = ("SELECT status, COUNT(*) AS n FROM orders WHERE created_at >= '2024-06-01' "
             "AND status <> 'cancelled' GROUP BY status")
    v = decide(HAVING, backend=synthetic_wh, tracer=Tracer(), llm=FakeLLM(wrong))
    assert v.decision == "warn"
    assert any("llm rewrite failed verification" in f for f in v.findings)
    assert v.explanation == "plain-language explanation"  # F3.3: warn gets explained


def test_deterministic_fixer_runs_before_the_llm(synthetic_wh):
    llm = FakeLLM("SELECT 1")
    v = decide(WRAPPED, backend=synthetic_wh, tracer=Tracer(), llm=llm)
    assert v.decision == "rewrite" and v.rewrite_source == "deterministic:sargable"


def test_llm_not_called_on_clean_queries(synthetic_wh):
    llm = FakeLLM("SELECT 1")
    v = decide("SELECT id FROM users WHERE id = 1", backend=synthetic_wh, tracer=Tracer(), llm=llm)
    assert v.decision == "allow" and llm.explained == 0 and v.explanation is None


def test_llm_rewrite_that_writes_is_refused(synthetic_wh):
    v = decide(HAVING, backend=synthetic_wh, tracer=Tracer(), llm=FakeLLM("DROP TABLE orders"))
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


# ---- Resubmitted rewrites: one round per query ------------------------------

CTE = ("SELECT * FROM (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) a "
       "JOIN (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) b "
       "USING (user_id) ORDER BY user_id LIMIT 50")
PUSH = ("SELECT t.order_id, t.country FROM (SELECT o.order_id, o.created_at, u.country "
        "FROM orders o JOIN users u ON u.id = o.user_id) t "
        "WHERE t.created_at >= '2024-06-01' AND t.country = 'US'")


@pytest.mark.parametrize("sql, fixer", [(WRAPPED, "sargable"), (PUSH, "pushdown"), (CTE, "cte")])
def test_catalog_rewrite_is_not_rewritten_again(synthetic_wh, sql, fixer):
    """Pins today's behavior with no log at all: a catalog fix removes the
    pattern it fixed, so its output never earns a second rewrite."""
    v = decide(sql, backend=synthetic_wh, tracer=Tracer())
    assert v.decision == "rewrite" and fixer in v.rewrite_source
    again = decide(v.rewritten_sql, backend=synthetic_wh, tracer=Tracer())
    assert again.decision in ("allow", "warn") and again.rewritten_sql is None


class PaddingLLM(FakeLLM):
    """Proposes an equivalent query that keeps the flagged HAVING, so every
    resubmission is still flagged and gets a fresh "improvement"."""

    def __init__(self):
        super().__init__(None)

    def propose_rewrite(self, sql, findings):
        return RewriteProposal(sql, sql.replace("GROUP BY", "AND 1 = 1 GROUP BY", 1), "fake")


def test_llm_would_rewrite_its_own_rewrite_without_the_log(synthetic_wh):
    first = decide(HAVING, backend=synthetic_wh, tracer=Tracer(), llm=PaddingLLM())
    assert first.decision == "rewrite"
    second = decide(first.rewritten_sql, backend=synthetic_wh, tracer=Tracer(), llm=PaddingLLM())
    assert second.decision == "rewrite"  # the loop the offered-rewrite check exists for
    stopped = decide(first.rewritten_sql, backend=synthetic_wh, tracer=Tracer(), llm=PaddingLLM(),
                     offered_rewrites={fingerprint_query(first.rewritten_sql)})
    assert stopped.decision == "warn" and stopped.rewritten_sql is None
    assert any("already offered" in f for f in stopped.findings)


def test_run_query_offers_one_rewrite_round(synthetic_wh, tmp_path):
    log = tmp_path / "log.jsonl"
    first = run_query(HAVING, synthetic_wh, log_path=log, tracer=Tracer(), llm=PaddingLLM())
    assert first.verdict.decision == "rewrite"
    second = run_query(first.verdict.rewritten_sql, synthetic_wh, log_path=log, tracer=Tracer(),
                       llm=PaddingLLM())
    assert second.verdict.decision == "warn" and second.execution.status == "completed"
    assert [r["decision"] for r in read_log(log)] == ["rewrite", "warn"]
    # The original query's own shape was never an offered rewrite: still offered.
    third = run_query(HAVING, synthetic_wh, log_path=log, tracer=Tracer(), llm=PaddingLLM())
    assert third.verdict.decision == "rewrite"


def test_load_dotenv_sets_only_missing_nonempty_and_never_overrides(tmp_path, monkeypatch):
    from cost_guard.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text("# comment\nCG_A=one\nCG_B=\nexport CG_C=\"three\"\nCG_D=from_file\nnot a line\n")
    monkeypatch.delenv("CG_A", raising=False)
    monkeypatch.delenv("CG_B", raising=False)
    monkeypatch.delenv("CG_C", raising=False)
    monkeypatch.setenv("CG_D", "from_shell")
    assert sorted(load_dotenv(env)) == ["CG_A", "CG_C"]
    import os
    assert os.environ["CG_A"] == "one" and os.environ["CG_C"] == "three"
    assert "CG_B" not in os.environ and os.environ["CG_D"] == "from_shell"
    assert load_dotenv(tmp_path / "missing.env") == []
