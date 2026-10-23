"""LLM suggestions for warned or cost-blocked queries (2026-10-18): checked
(read-only, valid by dry run), priced against the original, never run or
swapped in; one that claims an unchanged result is tried as a rewrite."""
from types import SimpleNamespace

import pytest

from query_preflight import guard
from query_preflight.backends.bigquery_backend import BigQueryWarehouse
from query_preflight.guard import assist, decide
from query_preflight.llm import SuggestionOut
from query_preflight.tracing import Tracer

GiB = 1024**3
SQL = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"  # no partition filter -> warn
BETTER = ("SELECT status, COUNT(*) AS n FROM orders WHERE created_at >= "
          "TIMESTAMP '2026-07-01' GROUP BY status")



def _check(sql, llm=None, **kw):
    """The verdict (decide, no model), then the model's help (assist)."""
    v = decide(sql, **kw)
    return assist(sql, v, llm, customer=kw.get("customer"), backend=kw.get("backend"),
                  tracer=kw.get("tracer"))

class Client:
    """orders, partitioned on created_at: 4 GiB unfiltered, 1 GiB with a date filter."""

    def query(self, sql, job_config=None):
        if "DELETE" in sql.upper():
            raise RuntimeError("not a query")
        nbytes = 1 * GiB if "created_at >=" in sql else 4 * GiB
        return SimpleNamespace(total_bytes_processed=nbytes)

    def get_table(self, ref):
        return SimpleNamespace(table_type="TABLE", num_rows=125_584, num_bytes=4 * GiB,
                               schema=[SimpleNamespace(name=c) for c in ("status", "created_at")],
                               time_partitioning=SimpleNamespace(field="created_at"),
                               range_partitioning=None, clustering_fields=None)


class FakeLLM:
    def __init__(self, sql=BETTER, changes_result=True):
        self.out = SuggestionOut(improved_sql=sql, changes=["adds a created_at range: last 3 months only"],
                                 changes_result=changes_result, runtime_note="reads 1 of 4 GiB")
        self.calls = 0

    def suggest_improvement(self, sql, items):
        self.calls += 1
        return self.out

    def explain_flag(self, sql, items):
        return "explained"


def _customer(cap=200):
    return {"workload_classes": {"interactive": {"max_bytes_scanned": 200 * GiB, "max_runtime_sec": 60}},
            "cost_model": {"price_per_tb_usd": 6.25}, "llm": {"suggestions_per_day": cap}}


@pytest.fixture(autouse=True)
def fresh():
    guard._suggest_cache.clear()
    guard._suggest_day.update(day=None, n=0)


def _wh(tmp_path):
    return BigQueryWarehouse("p", "ds", client=Client(), registry_path=str(tmp_path / "none"))


def test_a_warned_query_gets_a_priced_suggestion(tmp_path):
    llm = FakeLLM()
    v = _check(SQL, customer=_customer(), backend=_wh(tmp_path), tracer=Tracer(), llm=llm)
    assert v.decision == "warn" and v.rewritten_sql is None  # the decision stands
    s = v.suggestion
    assert s["sql"] == BETTER and s["changes_result"] and s["changes"]
    assert s["bytes_billed"] == {"original": 4 * GiB, "suggested": 1 * GiB}
    assert s["saves_usd_per_run_modeled"] == pytest.approx(3 * GiB / 1024**4 * 6.25)
    assert s["runtime_note"].startswith("(the model's reasoning, not measured)")
    assert llm.calls == 1  # one call per query, cached for a repeat
    _check(SQL, customer=_customer(), backend=_wh(tmp_path), tracer=Tracer(), llm=llm)
    assert llm.calls == 1


def test_an_unchanged_result_claim_is_tried_as_a_rewrite_and_reported(tmp_path):
    """Claimed equivalent: tried as a rewrite. Here its sampled check can't
    run (fake warehouse), so it is not offered, and the suggestion says so."""
    v = _check(SQL, customer=_customer(), backend=_wh(tmp_path), tracer=Tracer(),
               llm=FakeLLM(changes_result=False))
    assert v.decision == "warn"
    assert "sample check found a difference" in v.suggestion["equivalence"]


def test_invalid_or_writing_suggestions_are_not_shown(tmp_path):
    for bad in ("DELETE FROM orders WHERE 1 = 1", "SELECT nope FROM orders WHERE created_at >= x"):
        guard._suggest_cache.clear()
        llm = FakeLLM(sql=bad)
        if "nope" in bad:  # the warehouse rejects it at the dry run
            llm.out.improved_sql = "SELECT DELETE_ME"
        v = _check(SQL, customer=_customer(), backend=_wh(tmp_path), tracer=Tracer(), llm=llm)
        assert v.decision == "warn" and v.suggestion is None, bad


def test_the_daily_cap_is_a_visible_skip(tmp_path):
    llm = FakeLLM()
    v = _check(SQL, customer=_customer(cap=0), backend=_wh(tmp_path), tracer=Tracer(), llm=llm)
    assert llm.calls == 0 and v.suggestion is None
    assert "llm_suggestion: daily cap of 0 reached" in v.skipped_rules


def test_a_cost_block_gets_a_suggestion_too(tmp_path):
    c = _customer()
    c["workload_classes"]["interactive"]["max_bytes_scanned"] = 2 * GiB  # 4 GiB query: blocked
    v = _check(SQL, customer=c, backend=_wh(tmp_path), tracer=Tracer(), llm=FakeLLM())
    assert v.decision == "block" and v.suggestion["bytes_billed"]["suggested"] == 1 * GiB


def test_no_llm_no_suggestion(tmp_path):
    v = decide(SQL, customer=_customer(), backend=_wh(tmp_path), tracer=Tracer())
    assert v.decision == "warn" and v.suggestion is None
