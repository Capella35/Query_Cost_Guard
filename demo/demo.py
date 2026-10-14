#!/usr/bin/env python3
"""Cost Guard + Investigator — 5-minute demo. Zero setup.

Builds a synthetic e-commerce warehouse in DuckDB (/tmp) and walks through
four beats:

  [1] INVISIBLE COST — SELECT * on a wide table       -> WARN
  [2] RUNAWAY JOIN   — accidental cartesian product    -> BLOCK
  [3] SAFE REWRITE   — repeated subquery -> CTE        -> REWRITE (sample-verified)
  [4] THE LOOP       — Investigator learns a threshold from history

Run:  PYTHONPATH=src python3 demo/demo.py
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "demo"))

from warehouse import build_synthetic_warehouse
from cost_guard.guard import decide
from cost_guard.config import caps_for, load_customer
from cost_guard.investigator import THRESHOLD_HEADROOM, propose_threshold
from cost_guard.thresholds import BaselineStore, fingerprint_query

DB = "/tmp/cost_guard_demo.duckdb"


def beat(n: int, title: str) -> None:
    print(f"\n{'=' * 68}\n[{n}/4] {title}\n{'=' * 68}")


def show(sql: str, verdict) -> None:
    print(f"  query   : {' '.join(sql.split())[:100]}")
    print(f"  verdict : {verdict.decision.upper()}")
    print(f"  reason  : {verdict.reason}")
    for f in verdict.findings:
        print(f"  finding : {f}")


def main() -> None:
    print("building synthetic warehouse (users, orders, order_items, events)...")
    t0 = time.time()
    wh = build_synthetic_warehouse(DB)
    print(f"  done in {time.time() - t0:.1f}s -> {DB}")

    # ---- [1] INVISIBLE COST -------------------------------------------------
    beat(1, "INVISIBLE COST — the analyst can't see what this scans")
    q1 = "SELECT * FROM events WHERE created_at >= '2024-06-01'"
    est = wh.estimate(q1)
    print(f"  estimate: {est.detail}")
    show(q1, decide(q1, backend=wh))
    print("  -> in production this is the moment the invoice surprise is born.")

    # ---- [2] RUNAWAY JOIN ---------------------------------------------------
    beat(2, "RUNAWAY JOIN — the query that ruins the morning")
    q2 = "SELECT * FROM users, orders"
    est = wh.estimate(q2)
    print(f"  estimate: {est.detail}")
    show(q2, decide(q2, backend=wh))
    print("  -> blocked before a single byte is scanned. No invoice, no incident.")

    # ---- [3] SAFE REWRITE ---------------------------------------------------
    beat(3, "SAFE REWRITE — same answer on a verified sample")
    q3_bad = """SELECT * FROM
      (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) a
      JOIN (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) b
      USING (user_id) ORDER BY user_id LIMIT 50"""
    v3 = decide(q3_bad, backend=wh)
    show(q3_bad, v3)
    if v3.decision == "rewrite" and v3.rewritten_sql:
        print(f"  suggested: {' '.join(v3.rewritten_sql.split())[:110]}")
        t0 = time.time()
        wh._connect().execute(q3_bad).fetchall()
        t_bad = time.time() - t0
        t0 = time.time()
        wh._connect().execute(v3.rewritten_sql).fetchall()
        t_good = time.time() - t0
        print(f"  timing  : original {t_bad:.2f}s vs suggested {t_good:.2f}s")
        print(f"  verified: {v3.verification}")
        print(
            "  -> verified on a sample before it is offered. DuckDB may already share\n"
            "     the repeated scan (similar timings); engines that re-run each\n"
            "     subquery are where the CTE saves the second scan."
        )
    else:
        print("  (no verified rewrite offered)")

    # ---- [4] THE LOOP -------------------------------------------------------
    beat(4, "THE LOOP — the Investigator learns what 'expensive' means here")
    q4a = "SELECT name FROM users WHERE id = 42"
    q4b = "SELECT name FROM users WHERE id = 9001"
    same = fingerprint_query(q4a) == fingerprint_query(q4b)
    print(f"  fingerprint: two queries, different literals -> same shape: {same}")
    print(f"    '{fingerprint_query(q4a)}'")
    store = BaselineStore(min_runs_for_adaptive=20)
    random.seed(7)
    for _ in range(40):  # the normal past: fast, boring (enough runs for a robust p95)
        store.record(q4a, 50_000, max(0.05, random.gauss(0.35, 0.05)))
    store.record(q4a, 50_000, 3.1)  # the bad old days
    store.record(q4a, 50_000, 4.2)
    stats = store.baseline(q4a)
    print(f"  history : {stats.run_count} runs of this query shape")
    print(f"  learned : median {stats.median_secs:.2f}s, p95 {stats.p95_secs:.2f}s")
    hard_cap = caps_for(load_customer(), "interactive")["max_runtime_sec"]
    target = stats.p95_secs * THRESHOLD_HEADROOM
    new_threshold = propose_threshold(stats, hard_cap, hard_cap)
    steps, t = 1, new_threshold
    while t > target * 1.0001:
        t, steps = propose_threshold(stats, t, hard_cap) or target, steps + 1
    print(f"  proposal: kill this shape above {new_threshold:g}s (was the {hard_cap}s "
          f"interactive hard cap from customer.yaml)")
    print(f"  ratchet : target is p95 x {THRESHOLD_HEADROOM} = {target:.2f}s, but one update "
          f"moves at most 25% — about {steps} shadow-tested updates to get there")
    print("  next    : safety gates -> (optional LLM judge) -> shadow trial -> enforce,")
    print("            with auto-rollback if the block rate jumps.")

    print(
        f"\n{'=' * 68}\nDone. Guard checked 3 queries before execution; "
        "Investigator proposed 1 threshold.\nNo cloud account was harmed."
    )


if __name__ == "__main__":
    main()
