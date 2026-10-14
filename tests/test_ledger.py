"""Workload report (ledger.py): warn follow-through, cost by shape, result-cache
opportunity, spend ledger — from hand-built logs and from real run_query logs."""
import json
from datetime import datetime, timedelta, timezone

from cost_guard.config import load_customer
from cost_guard.ledger import build_report
from cost_guard.runtime import query_hash, read_log, run_query
from cost_guard.tracing import Tracer

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _rec(minute, fp, decision="allow", status="completed", user="u1", dur=1.0, rows=1000,
         sql_hash=None, rewrite_fp=None, bytes_=None, bypassed=False):
    return {"ts": (T0 + timedelta(minutes=minute)).isoformat(), "fingerprint": fp,
            "workload_class": "interactive", "decision": decision, "status": status,
            "duration_sec": None if status == "blocked" else dur, "rows_estimate": rows,
            "timeout_sec": 60, "user": user, "sql_hash": sql_hash,
            "rewrite_fingerprint": rewrite_fp, "bytes_estimate": bytes_, "bypassed": bypassed}


def _customer(**cost):
    cfg = load_customer()
    cfg["cost_model"] = {"price_per_tb_usd": cost.get("tb"), "price_per_runtime_hour_usd": cost.get("hour")}
    return cfg


def test_warn_followthrough_classifies_next_run():
    log = [
        _rec(0, "a", "rewrite", user="u1", rewrite_fp="a_fixed"), _rec(2, "a_fixed", user="u1"),  # adopted
        _rec(0, "b", "warn", user="u2"), _rec(5, "b2", user="u2"),                                # changed to clean
        _rec(0, "c", "warn", user="u3"), _rec(1, "c", "warn", user="u3"),  # ignored; 2nd warn: no follow-up
        _rec(0, "d", "warn", user="u4"), _rec(90, "d2", user="u4"),        # next run outside the window
        _rec(0, "e", "warn", user=None),                                                          # no user
    ]
    ft = build_report(log, _customer())["warn_followthrough"]
    assert ft["adopted_rewrite"] == 1 and ft["changed_to_clean"] == 1
    assert ft["reran_unchanged"] == 1
    assert ft["no_followup"] == 2
    assert ft["flagged_runs_without_user"] == 1
    assert ft["flagged_runs_with_user"] == 5
    assert ft["acted_rate_upper_bound"] == 0.4 and ft["ignored_rate"] == 0.2


def test_workload_ranks_repeated_shape_above_one_off():
    log = [_rec(i, "dashboard", user=f"u{i % 5}", dur=2.0) for i in range(50)]
    log += [_rec(0, "one_off_big", dur=30.0)]
    shapes = build_report(log, _customer(hour=4.0))["workload_by_shape"]
    assert shapes[0]["fingerprint"] == "dashboard" and shapes[0]["users"] == 5
    assert shapes[0]["modeled_cost_usd"] == round(100 / 3600 * 4.0, 4)
    assert shapes[0]["runtime_share"] == round(100 / 130, 3)


def test_bytes_price_wins_over_runtime_price():
    log = [_rec(0, "x", bytes_=2 * 1024**4, dur=10.0)]
    rep = build_report(log, _customer(tb=6.25, hour=4.0))
    assert rep["cost_unit"].startswith("MODELED USD from bytes")
    assert rep["workload_by_shape"][0]["modeled_cost_usd"] == 12.5


def test_no_price_reports_seconds():
    rep = build_report([_rec(0, "x")], _customer())
    assert rep["cost_unit"].startswith("runtime seconds")
    assert rep["workload_by_shape"][0]["modeled_cost_usd"] is None


def test_cache_opportunity_counts_identical_repeats_within_ttl():
    log = [_rec(0, "q", sql_hash="h1", dur=3.0), _rec(30, "q", sql_hash="h1", dur=3.0),  # repeat
           _rec(200, "q", sql_hash="h1", dur=3.0),                                     # past 60 min TTL
           _rec(31, "q", sql_hash="h2", dur=3.0)]                                      # same shape, other literals
    c = build_report(log, _customer())["cache_opportunity"]
    assert c["repeat_within_ttl"] == 1 and c["runtime_sec_recomputed"] == 3.0
    assert c["repeat_rate"] == 0.25


def test_spend_ledger_and_false_block_proxy():
    log = [_rec(0, "ok", dur=1.0), _rec(1, "slow", status="killed", dur=60.0),
           _rec(2, "huge", "block", status="blocked", rows=9_000_000),
           _rec(3, "huge", "warn", dur=5.0, bypassed=True)]
    led = build_report(log, _customer())["spend_ledger"]
    assert led["executed"]["runs"] == 3 and led["killed"]["runtime_sec_before_kill"] == 60.0
    assert led["blocked"]["rows_estimate_not_run"] == 9_000_000
    assert led["bypassed"] == 1 and led["false_block_proxy_shapes"] == 1
    assert list(led["by_week"]) == ["2026-W40"]


def test_run_query_logs_report_fields(synthetic_wh, tmp_path, monkeypatch):
    monkeypatch.delenv("COST_GUARD_LOG_KEY", raising=False)
    log = tmp_path / "log.jsonl"
    q = "SELECT id FROM users WHERE email = 'alice@example.com'"
    run_query(q, synthetic_wh, log_path=log, tracer=Tracer(), user="analyst-7")
    run_query(q, synthetic_wh, log_path=log, tracer=Tracer(), user="analyst-7")
    recs = read_log(log)
    assert recs[0]["user"] == "analyst-7" and recs[0]["sql_hash"] == recs[1]["sql_hash"]
    assert "alice" not in json.dumps(recs)  # keyed hash, not the literal
    assert (tmp_path / ".log_key").exists()
    assert build_report(recs, load_customer())["cache_opportunity"]["repeat_within_ttl"] == 1


def test_query_hash_is_keyed(tmp_path, monkeypatch):
    monkeypatch.setenv("COST_GUARD_LOG_KEY", "k1")
    a = query_hash("SELECT 1", tmp_path / "l.jsonl")
    monkeypatch.setenv("COST_GUARD_LOG_KEY", "k2")
    assert query_hash("SELECT 1", tmp_path / "l.jsonl") != a
    assert query_hash("SELECT  1", tmp_path / "l.jsonl") == query_hash("SELECT 1", tmp_path / "l.jsonl")


def test_run_logs_offered_rewrite_shape(synthetic_wh, tmp_path):
    log = tmp_path / "log.jsonl"
    q = ("SELECT order_id FROM orders WHERE DATE_TRUNC('month', created_at) = DATE '2024-03-01'")
    out = run_query(q, synthetic_wh, log_path=log, tracer=Tracer(), user="u")
    assert out.verdict.decision == "rewrite"
    rec = read_log(log)[0]
    assert rec["rewrite_fingerprint"] and rec["rewrite_fingerprint"] != rec["fingerprint"]
    # Running the offered rewrite next counts as adopting it.
    run_query(out.verdict.rewritten_sql, synthetic_wh, log_path=log, tracer=Tracer(), user="u")
    assert build_report(read_log(log), load_customer())["warn_followthrough"]["adopted_rewrite"] == 1


def test_cli_report(tmp_path, capsys):
    from cost_guard.__main__ import main

    log = tmp_path / "log.jsonl"
    log.write_text("\n".join(json.dumps(_rec(i, "s")) for i in range(3)) + "\n")
    assert main(["report", "--log", str(log)]) == 0
    assert json.loads(capsys.readouterr().out)["spend_ledger"]["runs"] == 3
