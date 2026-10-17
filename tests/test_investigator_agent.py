"""Anomaly trigger in run_query + the report-only investigator agent.

A fake warehouse with controlled run times and sizes makes anomalies
deterministic: 25 normal runs of a shape, then one that breaks its history."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from cost_guard.anomaly import COLD_START_RUNS, check_run
from cost_guard.backends.base import ExecutionResult, PlanEstimate
from cost_guard.config import load_customer
from cost_guard.investigator_agent import InvestigatorAgent, diagnose, events_from_log
from cost_guard.mcp_server import CostGuardService
from cost_guard.runtime import append_log, LogRecord, read_log, run_query

SQL = "SELECT status, COUNT(*) FROM orders WHERE user_id = 4242 GROUP BY status"
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class FakeWarehouse:
    name = "fake"

    def __init__(self):
        self.next_duration = 0.10
        self.next_rows = 1000

    def estimate(self, sql, two_way_ranges=False):
        return PlanEstimate(estimated_rows_scanned=self.next_rows, estimated_bytes_scanned=None)

    def table_metadata(self, table):
        return {"table": table, "num_rows": 5000, "column_count": 4, "columns": ["status", "user_id"]}

    def execute(self, sql, timeout_sec=None):
        return ExecutionResult("completed", self.next_duration, row_count=3)

    def column_registry(self):
        return None


def _customer(**inv):
    cfg = load_customer()
    cfg["investigator"] = {"enabled": True, "cooldown_min": 60, "max_per_day": 20,
                           "llm_max_per_day": 0, **inv}
    return cfg


def _seed(log, n=25, dur=0.10, rows=1000, fp=None, start=T0):
    from cost_guard.thresholds import fingerprint_query

    for i in range(n):
        append_log(LogRecord(ts=(start + timedelta(minutes=i)).isoformat(),
                             fingerprint=fp or fingerprint_query(SQL), workload_class="interactive",
                             decision="allow", status="completed",
                             duration_sec=dur * (1 + 0.02 * (i % 5)), rows_estimate=rows,
                             timeout_sec=60, anomaly_size=rows), log)


def test_check_run_cold_start_then_runtime_and_size(tmp_path):
    log = tmp_path / "log.jsonl"
    _seed(log, n=COLD_START_RUNS - 1)
    fp = read_log(log)[0]["fingerprint"]
    now = (T0 + timedelta(hours=2)).isoformat()
    assert check_run(read_log(log), fp, now, 1000, 1.0) is None  # cold start
    _seed(log, n=6, start=T0 + timedelta(minutes=30))
    assert check_run(read_log(log), fp, now, 1000, 0.105) is None  # normal
    a = check_run(read_log(log), fp, now, 1000, 1.0)
    assert a.kind == "runtime" and a.history_runs == 25
    assert check_run(read_log(log), fp, now, 50_000, 0.105).kind == "size"


def test_run_query_logs_the_flag_and_hands_over_the_event(tmp_path):
    log, wh = tmp_path / "log.jsonl", FakeWarehouse()
    _seed(log)
    events = []
    wh.next_duration = 1.5
    out = run_query(SQL, wh, customer=_customer(), log_path=log, on_anomaly=events.append)
    assert out.anomaly is not None and out.anomaly.kind == "runtime"
    rec = read_log(log)[-1]
    assert rec["anomaly"].startswith("adaptive anomaly: runtime") and rec["anomaly_size"] == 1000
    assert events and events[0]["sql"] == SQL  # in memory, for the agent's check
    assert "4242" not in json.dumps(rec)  # the log keeps no literals


@pytest.fixture
def service(tmp_path):
    return CostGuardService(FakeWarehouse(), _customer(), log_path=tmp_path / "log.jsonl",
                            trace_path=tmp_path / "traces.jsonl")


def _wait(svc):
    for f in svc.pending:
        f.result(timeout=30)


def test_anomaly_starts_a_report_only_investigation(service, tmp_path):
    _seed(service.log_path)
    normal = service.run_query(SQL)
    assert normal["anomaly"] is None and normal["investigation"] is None
    service.backend.next_duration = 1.5
    out = service.run_query(SQL)
    assert out["anomaly"] and out["investigation"] == "started"
    _wait(service)
    reports = list((tmp_path / "investigations").glob("*.json"))
    assert len(reports) == 1
    rep = json.loads(reports[0].read_text())
    assert rep["changes_made"] == [] and rep["anomaly_kind"] == "runtime"
    assert rep["likely_causes"] and rep["look_next"]
    assert [s.split(":")[0] for s in rep["steps"]] == [
        "history", "tables", "check", "neighbours", "memory", "diagnose"]
    assert rep["facts"]["tables"]["orders"]["num_rows"] == 5000
    md = reports[0].with_suffix(".md").read_text()
    assert "Changes made: none" in md
    # No SQL text or literals in the report.
    for text in (reports[0].read_text(), md):
        assert "4242" not in text and "SELECT status" not in text
    # One trace: agent.investigate with the tool calls under it.
    spans = [json.loads(line) for line in service.trace_path.read_text().splitlines()]
    agent = next(s for s in spans if s["name"] == "agent.investigate")
    assert agent["trace_id"] == rep["trace_id"]
    under = {s["name"] for s in spans if s["trace_id"] == agent["trace_id"]}
    assert {"agent.step.history", "mcp.get_query_history", "mcp.check_query",
            "agent.step.diagnose"} <= under
    # Incident remembered for next time.
    assert (tmp_path / "investigations" / "incidents.jsonl").exists()


def test_cooldown_and_daily_cap(service, tmp_path):
    _seed(service.log_path)
    service.backend.next_duration = 1.5
    assert service.run_query(SQL)["investigation"] == "started"
    _wait(service)
    # The first spike is now history: the drift baseline's bar is the largest
    # past residual, so the next flag needs a bigger excess.
    service.backend.next_duration = 6.0
    assert service.run_query(SQL)["investigation"].startswith("skipped: cooldown")
    agent = InvestigatorAgent(service, _customer(max_per_day=1), tmp_path / "investigations")
    ok, why = agent.should_investigate("another shape", datetime.now(timezone.utc).isoformat())
    assert not ok and "daily cap" in why


def test_batch_from_log_skips_the_check(service, tmp_path):
    _seed(service.log_path)
    service.agent = None  # live trigger off: the anomaly is only logged
    service.backend.next_duration = 1.5
    assert service.run_query(SQL)["investigation"] == "off"
    agent = InvestigatorAgent(service, _customer(), tmp_path / "inv")
    (ev,) = events_from_log(read_log(service.log_path))
    rep = agent.investigate(ev)
    assert any(s.startswith("check: skipped") for s in rep.steps)
    assert agent.investigate(ev) is None  # already investigated


def test_diagnose_tells_warehouse_load_from_the_shape():
    wide = diagnose("runtime", {}, {"neighbours": {"window_min": 30, "runs": 6, "shapes": 3,
                                                   "slow_runs": 5, "slow_shapes": 3}})
    assert "warehouse-wide" in wide[0][0]
    alone = diagnose("runtime", {}, {"neighbours": {"window_min": 30, "runs": 6, "shapes": 3,
                                                    "slow_runs": 0, "slow_shapes": 0}})
    assert "this shape alone" in alone[0][0]
    size = diagnose("size", {}, {"check": {"rules_fired": ["require_partition_filter"]}})
    assert "different constants" in size[0][0] and "partition" in size[0][1]


def test_llm_summary_is_capped_and_cached(service, tmp_path):
    class FakeLLM:
        calls = 0

        def summarize_investigation(self, facts):
            FakeLLM.calls += 1
            assert "SELECT" not in json.dumps(facts, default=str)
            return "The shape ran 15x slower than usual."

    _seed(service.log_path)
    agent = InvestigatorAgent(service, _customer(llm_max_per_day=1, cooldown_min=0),
                              tmp_path / "inv", llm=FakeLLM())
    service.backend.next_duration = 1.5
    service.agent = None
    service.run_query(SQL)
    (ev,) = events_from_log(read_log(service.log_path))
    rep = agent.investigate(ev)
    assert rep.summary_source == "llm" and FakeLLM.calls == 1
    # Re-running the identical investigation (same facts) hits the cache.
    for f in (tmp_path / "inv").glob(f"{rep.id}.*"):
        f.unlink()
    (tmp_path / "inv" / "incidents.jsonl").unlink()
    again = agent.investigate(ev)
    assert again.summary_source == "llm-cache" and FakeLLM.calls == 1
    # Different facts, cap of 1 per day already used: no call.
    other = agent.investigate({**ev, "ts": (datetime.fromisoformat(ev["ts"]) + timedelta(seconds=2)).isoformat(),
                               "duration_sec": 9.9, "reason": "different"})
    assert other.summary is None and FakeLLM.calls == 1
    assert any("LLM cap" in s for s in other.steps)


def test_trend_is_not_extrapolated_across_a_long_gap(tmp_path):
    # 25 runs in 25 minutes, then a run 16 days later: the fitted trend must
    # not turn into an astronomically high "expected" runtime.
    log = tmp_path / "log.jsonl"
    _seed(log)
    fp = read_log(log)[0]["fingerprint"]
    later = (T0 + timedelta(days=16)).isoformat()
    assert check_run(read_log(log), fp, later, 1000, 1.0).kind == "runtime"
    assert check_run(read_log(log), fp, later, 1000, 0.105) is None


def test_option_b_keeps_the_bar_after_a_spike(tmp_path):
    # Option A: the first 1.5 s spike joins the history and hides the second.
    # Option B (customer.yaml anomaly.exclude_flagged): the second is caught too.
    wh = FakeWarehouse()
    wh.next_duration = 1.5
    for option, second_flagged in ((False, False), (True, True)):
        log = tmp_path / f"log_{option}.jsonl"
        _seed(log)
        cust = {**_customer(), "anomaly": {"exclude_flagged": option}}
        assert run_query(SQL, wh, customer=cust, log_path=log).anomaly is not None
        assert (run_query(SQL, wh, customer=cust, log_path=log).anomaly is not None) is second_flagged


def _b():
    return {**_customer(), "anomaly": {"exclude_flagged": True}}


def test_accept_normal_ends_the_flags_for_a_lasting_change(tmp_path):
    from cost_guard.anomaly import accept_normal, read_acceptances
    from cost_guard.thresholds import fingerprint_query, shape_id

    log, wh = tmp_path / "log.jsonl", FakeWarehouse()
    _seed(log)
    wh.next_duration = 1.5  # the shape is 15x slower from now on, legitimately
    flags = [run_query(SQL, wh, customer=_b(), log_path=log).anomaly is not None for _ in range(3)]
    assert flags == [True, True, True]  # option B: flagged every time
    acc = accept_normal(log, shape_id(fingerprint_query(SQL))[:8], by="analyst-1", note="new index")
    assert acc.flagged_runs == 3 and read_acceptances(log)[0]["by"] == "analyst-1"
    assert run_query(SQL, wh, customer=_b(), log_path=log).anomaly is None  # the new normal
    wh.next_duration = 30.0  # a real spike after the acceptance is still caught
    assert run_query(SQL, wh, customer=_b(), log_path=log).anomaly is not None


def test_accept_normal_needs_a_known_unique_shape(tmp_path):
    from cost_guard.anomaly import accept_normal

    log = tmp_path / "log.jsonl"
    _seed(log)
    with pytest.raises(ValueError, match="at least 6"):
        accept_normal(log, "abc")
    with pytest.raises(ValueError, match="no query shape"):
        accept_normal(log, "ffffffffffff")


def test_accept_normal_cli(tmp_path, capsys):
    from cost_guard.__main__ import main
    from cost_guard.thresholds import fingerprint_query, shape_id

    log = tmp_path / "log.jsonl"
    _seed(log)
    sid = shape_id(fingerprint_query(SQL))
    assert main(["accept-normal", sid, "--log", str(log), "--by", "me", "--note", "ok"]) == 0
    assert "accepted " + sid in capsys.readouterr().out
    assert main(["accept-normal", "--list", "--log", str(log)]) == 0
    assert sid in capsys.readouterr().out
    assert main(["accept-normal", "zzzzzzzz", "--log", str(log)]) == 1


def test_lasting_change_note_counts_only_flags_since_acceptance(tmp_path):
    from cost_guard.anomaly import accept_normal
    from cost_guard.thresholds import fingerprint_query, shape_id

    svc = CostGuardService(FakeWarehouse(), _b(), log_path=tmp_path / "log.jsonl",
                           trace_path=None, investigations=False)
    _seed(svc.log_path)
    svc.backend.next_duration = 1.5
    for _ in range(3):
        svc.run_query(SQL)
    agent = InvestigatorAgent(svc, _b() | {"investigator": {"cooldown_min": 0}}, tmp_path / "inv")
    (*_, last) = events_from_log(read_log(svc.log_path))
    rep = agent.investigate(last)
    assert rep.likely_causes[0].startswith("looks like a lasting change")
    assert rep.look_next[0].startswith("if this is the new normal: python -m cost_guard accept-normal "
                                       + rep.shape_id)
    accept_normal(svc.log_path, rep.shape_id)
    later = {**last, "ts": datetime.now(timezone.utc).isoformat()}
    rep2 = agent.investigate(later)
    assert rep2.facts["history"]["anomalies_in_last_10"] == 0
    assert rep2.facts["history"]["accepted_normal_at"] is not None
    assert not rep2.likely_causes[0].startswith("looks like a lasting change")
