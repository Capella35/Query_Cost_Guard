"""F0.2: caps come from customer.yaml, per workload class. F0.3: skipped rules
are visible on every verdict."""
from pathlib import Path

import pytest
import yaml

from cost_guard import guard
from cost_guard.config import caps_for, load_customer
from cost_guard.guard import decide
from cost_guard.tracing import Tracer

ROOT = Path(__file__).resolve().parent.parent

# Synthetic orders x events join: EXPLAIN estimates ~2M rows — between the
# interactive (1M) and scheduled (10M) caps.
BETWEEN_CAPS = ("SELECT o.order_id, e.event_id FROM orders o JOIN events e "
                "ON e.user_id = o.user_id WHERE o.created_at >= '2024-06-01'")


def test_load_customer_has_distinct_caps():
    cfg = load_customer()
    inter, sched = caps_for(cfg, "interactive"), caps_for(cfg, "scheduled")
    assert inter["max_rows_processed"] < sched["max_rows_processed"]
    with pytest.raises(KeyError):
        caps_for(cfg, "interactve")  # typo must not fall back to looser caps


def test_interactive_vs_scheduled_differ(synthetic_wh):
    cfg = load_customer()
    rows = synthetic_wh.estimate(BETWEEN_CAPS).rows_processed
    assert (caps_for(cfg, "interactive")["max_rows_processed"] < rows
            <= caps_for(cfg, "scheduled")["max_rows_processed"])  # the query still fits the test
    v_i = decide(BETWEEN_CAPS, "interactive", backend=synthetic_wh, tracer=Tracer())
    v_s = decide(BETWEEN_CAPS, "scheduled", backend=synthetic_wh, tracer=Tracer())
    assert v_i.decision == "block" and "interactive hard cap" in v_i.reason
    assert v_s.decision != "block"
    assert v_i.caps != v_s.caps


def test_changing_cap_in_yaml_changes_verdict(synthetic_wh, tmp_path):
    q = "SELECT order_id FROM orders WHERE created_at >= '2024-01-01'"
    assert decide(q, backend=synthetic_wh, tracer=Tracer()).decision == "allow"
    cfg = yaml.safe_load((ROOT / "customer.yaml").read_text())
    cfg["workload_classes"]["interactive"]["max_rows_processed"] = 1_000
    p = tmp_path / "customer.yaml"
    p.write_text(yaml.safe_dump(cfg))
    v = decide(q, customer=load_customer(p), backend=synthetic_wh, tracer=Tracer())
    assert v.decision == "block"
    assert "1,000" in v.reason


def test_no_backend_skips_are_visible():
    t = Tracer()
    v = decide("SELECT id FROM users WHERE id = 1", tracer=t)
    # Every table-level rule is skipped for 'users', visibly.
    assert any(s.startswith("require_partition_filter on 'users': no backend")
               for s in v.skipped_rules)
    skipped_span = [s for s in t.spans if s["name"] == "rules.skipped"]
    assert skipped_span and skipped_span[0]["attrs"]["rules"] == v.skipped_rules


def test_unimplemented_rule_is_reported_not_swallowed(monkeypatch, synthetic_wh):
    def stub_rule(sql, md):
        raise NotImplementedError

    from cost_guard.rules import static_rules

    monkeypatch.setattr(static_rules, "ALL_RULES", static_rules.ALL_RULES + [stub_rule])
    v = decide("SELECT id FROM users WHERE id = 1", backend=synthetic_wh, tracer=Tracer())
    assert "stub_rule on 'users': not implemented" in v.skipped_rules


def test_full_coverage_reports_no_skips(synthetic_wh):
    v = decide("SELECT id, name FROM users WHERE id = 42", backend=synthetic_wh, tracer=Tracer())
    assert v.decision == "allow"
    assert v.skipped_rules == []


def test_bypass_requires_config_flag(synthetic_wh, tmp_path):
    q = "SELECT * FROM users, orders"
    v = decide(q, backend=synthetic_wh, tracer=Tracer(), bypass_reason="demo")
    assert v.decision == "block" and "allow_bypass is false" in v.reason
    cfg = load_customer()
    cfg["guard"] = {"allow_bypass": True}
    t = Tracer()
    v = decide(q, customer=cfg, backend=synthetic_wh, tracer=t, bypass_reason="demo")
    assert v.decision == "warn" and v.bypassed == "demo"
    assert any(s["name"] == "guard.bypass" for s in t.spans)
    # Writes are never bypassable.
    v = decide("DELETE FROM users", customer=cfg, tracer=Tracer(), bypass_reason="x")
    assert v.decision == "block"


def test_is_read_only_still_exported():
    assert guard.is_read_only("SELECT 1")
