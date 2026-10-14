#!/usr/bin/env python3
"""Offline 70/30 replay: static-only vs static+adaptive Guard.

The PROJECT_BRIEF's step-4 eval for the Layer 2 adaptive thresholds:
split the query log by time (70% history, 30% test), compute per-fingerprint
baselines (median/p95 of bytes scanned and duration) on the history, then
replay the test queries through the Guard twice:

  1. static-only  — guard.decide() as-is (Layer 1 rules);
  2. static+adaptive — same, plus the anomaly rule from thresholds.py:
     warn when a query runs far above its own fingerprint's history
     (above p95, or more than 3x the median). Cold start: the adaptive
     layer only fires for fingerprints with >= 20 history runs.

Traffic is synthetic and labeled (authored anomalies, not human ground
truth — the same caveat as the 11-case eval). Three benign/volatile
fingerprints (point lookup, narrow vs wide date windows on orders, narrow
vs wide monthly aggregations over orders) plus cartesian controls that the
static layer blocks in both modes. A wide-window run has the SAME
fingerprint as a narrow-window run (fingerprinting normalizes literals)
but scans ~100x the data — the case static rules cannot see.

Reports catch rate and false-positive rate per mode. Results go to
eval/results/replay_<timestamp>.json. Reads nothing from the network.

Run:  .venv/bin/python eval/run_replay.py
"""
from __future__ import annotations

import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "demo"))

from warehouse import build_synthetic_warehouse
from cost_guard.guard import decide
from eval_config import load_eval_customer  # noqa: E402
from cost_guard.thresholds import BaselineStore, FingerprintStats

EVAL_CUSTOMER = load_eval_customer()  # caps the replay labels were written under

EXEC_TIMEOUT_SEC = 45  # defensive; authored queries finish in << 1 s
COLD_START_RUNS = 20  # brief: static rules only below ~20 runs per fingerprint
ANOMALY_MEDIAN_MULT = 3.0  # brief: "above p95 or a few times the median"
MIN_ABS_DURATION_SEC = 0.02  # runtime is noisy at ms scale (caching, jitter):
    # the duration clause only fires above this floor, so a 3 ms point lookup
    # can never be flagged as an anomaly over a 1 ms median

rng = random.Random(20261013)


def gen_traffic() -> list[dict]:
    """Chronological, labeled query log. Each run: {ts, sql, label}."""
    runs: list[dict] = []
    base = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    def add(sql: str, label: str, day: int, minute_jitter: int):
        ts = base + timedelta(days=day, minutes=len(runs) + rng.randint(0, minute_jitter))
        runs.append({"ts": ts, "sql": sql, "label": label})

    # F1: point lookup on users — always cheap, always good (control).
    for i in range(30):
        add(f"SELECT id, name, email FROM users WHERE id = {rng.randint(1, 50000)}",
            "good", day=i // 10, minute_jitter=3)

    # F2: date-window scan on 1M-row orders. Narrow 7-day windows are cheap;
    # full-range windows scan everything. Same fingerprint, ~10x cost spread.
    for i in range(30):
        wide = (i % 5 == 4)  # 6 wide runs, spread through the log
        if wide:
            d1, d2 = "2023-01-01", "2025-01-01"
        else:
            start = rng.randint(0, 723)
            d1 = (datetime(2023, 1, 1) + timedelta(days=start)).date().isoformat()
            d2 = (datetime(2023, 1, 1) + timedelta(days=start + 7)).date().isoformat()
        add(
            f"SELECT order_id, status, amount_cents FROM orders "
            f"WHERE created_at >= '{d1}' AND created_at < '{d2}'",
            "bad" if wide else "good", day=i // 10, minute_jitter=3,
        )

    # F3: monthly revenue aggregate over the 1M-row orders table, with the same
    # narrow-vs-wide window trick. Cost is dominated by the orders scan, so the
    # wide runs cost ~10x the narrow ones. Static rules see a date filter and
    # stay quiet; GROUP BY 1 keeps the high-cardinality rule out of it.
    for i in range(30):
        wide = (i % 5 == 2)  # 6 wide runs, offset from F2's bad positions
        if wide:
            d1, d2 = "2023-01-01", "2025-01-01"
        else:
            start = rng.randint(0, 723)
            d1 = (datetime(2023, 1, 1) + timedelta(days=start)).date().isoformat()
            d2 = (datetime(2023, 1, 1) + timedelta(days=start + 7)).date().isoformat()
        add(
            "SELECT DATE_TRUNC('month', created_at) AS m, SUM(amount_cents) "
            "FROM orders "
            f"WHERE created_at >= '{d1}' AND created_at < '{d2}' "
            "GROUP BY 1 ORDER BY 1 LIMIT 30",
            "bad" if wide else "good", day=i // 10, minute_jitter=3,
        )

    # F4: cartesian controls — static blocks them in both modes.
    for i in range(6):
        add("SELECT u.name, o.order_id FROM users u, orders o", "bad",
            day=i // 2, minute_jitter=5)

    runs.sort(key=lambda r: r["ts"])
    return runs


def timed_execute(db_path: str, sql: str):
    """Run the SQL in a worker thread; returns duration_sec or None."""
    import duckdb

    def _run():
        con = duckdb.connect(db_path, read_only=True)
        try:
            t0 = time.perf_counter()
            con.execute(sql).fetchall()
            return time.perf_counter() - t0
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_run)
        try:
            return fut.result(timeout=EXEC_TIMEOUT_SEC)
        except Exception:
            return None


def anomaly_flag(stats: FingerprintStats | None, rows_scanned, duration_sec,
                min_runs: int = COLD_START_RUNS) -> str | None:
    """Layer 2 anomaly rule (brief §Layer 2): warn when far above the
    fingerprint's own history — above p95 or a few times the median.
    Cold start: needs >= min_runs history runs.

    rows_scanned is the DuckDB upper-bound row estimate, standing in for
    bytes scanned on this path (the brief's dry-run bytes on BigQuery)."""
    if stats is None or stats.run_count < min_runs:
        return None
    reasons = []
    if rows_scanned is not None:
        pb, mb = stats.p95_bytes, stats.median_bytes
        if (pb is not None and rows_scanned > pb) or (
            mb and rows_scanned > ANOMALY_MEDIAN_MULT * mb
        ):
            reasons.append(
                f"rows scanned {rows_scanned:,} above fingerprint history "
                f"(median {mb:,.0f}, p95 {pb:,.0f}, {stats.run_count} runs)"
            )
    if duration_sec is not None and duration_sec > MIN_ABS_DURATION_SEC:
        ps, ms = stats.p95_secs, stats.median_secs
        if (ps is not None and duration_sec > ps) or (
            ms and duration_sec > ANOMALY_MEDIAN_MULT * ms
        ):
            reasons.append(
                f"runtime {duration_sec:.2f}s above fingerprint history "
                f"(median {ms:.2f}s, p95 {ps:.2f}s, {stats.run_count} runs)"
            )
    if reasons:
        return "adaptive anomaly: " + "; ".join(reasons)
    return None


def split_chronological(log: list[dict], frac: float = 0.70):
    """Split by time: sort first so an unsorted log can't bias the cut."""
    ordered = sorted(log, key=lambda r: r["ts"])
    cut = int(len(ordered) * frac)
    return ordered[:cut], ordered[cut:]


def run_standard() -> None:
    print("building synthetic warehouse...")
    wh = build_synthetic_warehouse("/tmp/cg_replay.duckdb")
    traffic = gen_traffic()
    history, test = split_chronological(traffic)
    print(f"query log: {len(traffic)} runs "
          f"({len(history)} history / {len(test)} test, 70/30 by time)")

    # Phase 1: replay history through the Guard, record baselines.
    store = BaselineStore(min_runs_for_adaptive=COLD_START_RUNS)
    skipped = 0
    for rec in history:
        verdict = decide(rec["sql"], customer=EVAL_CUSTOMER, backend=wh)
        if verdict.decision == "block":
            continue  # never runs; contributes no runtime baseline
        est = wh.estimate(rec["sql"], two_way_ranges=True)  # anomaly input: compare runs, not caps
        dur = timed_execute("/tmp/cg_replay.duckdb", rec["sql"])
        if dur is None:
            skipped += 1
            continue
        store.record(rec["sql"], est.rows_processed, dur)
    cold = sum(
        1 for s in store.stats.values() if s.run_count >= COLD_START_RUNS
    )
    print(f"baselines: {len(store.stats)} fingerprints, "
          f"{cold} past the {COLD_START_RUNS}-run cold start; "
          f"{skipped} history executions skipped")

    # Phase 2: replay test queries through both modes.
    rows = []
    for rec in test:
        verdict = decide(rec["sql"], customer=EVAL_CUSTOMER, backend=wh)
        est = wh.estimate(rec["sql"], two_way_ranges=True)  # anomaly input: compare runs, not caps
        dur = None
        if verdict.decision != "block":
            dur = timed_execute("/tmp/cg_replay.duckdb", rec["sql"])
        static_caught = verdict.decision in ("warn", "block", "rewrite")
        flag = anomaly_flag(
            store.baseline(rec["sql"]),
            est.rows_processed,
            dur,
            min_runs=store.min_runs_for_adaptive,
        )
        rows.append(
            {
                "label": rec["label"],
                "sql": rec["sql"][:80],
                "static": verdict.decision,
                "static_caught": static_caught,
                "adaptive_flag": flag,
                "caught_adaptive": static_caught or flag is not None,
                "duration_sec": round(dur, 3) if dur is not None else None,
            }
        )

    def metrics(mode: str):
        bad = [r for r in rows if r["label"] == "bad"]
        good = [r for r in rows if r["label"] == "good"]
        caught = [r for r in bad if r["caught_adaptive" if mode == "adaptive" else "static_caught"]]
        fp = [r for r in good if r["caught_adaptive" if mode == "adaptive" else "static_caught"]]
        return {
            "bad_runs": len(bad),
            "good_runs": len(good),
            "catch_rate": round(len(caught) / len(bad), 3) if bad else None,
            "false_positive_rate": round(len(fp) / len(good), 3) if good else None,
        }

    m_static = metrics("static")
    m_adaptive = metrics("adaptive")

    print(f"\n{'mode':<18}{'bad':<6}{'good':<6}{'catch':<8}{'false-pos'}")
    for name, m in (("static-only", m_static), ("static+adaptive", m_adaptive)):
        print(
            f"{name:<18}{m['bad_runs']:<6}{m['good_runs']:<6}"
            f"{m['catch_rate']:<8}{m['false_positive_rate']}"
        )
    missed = [r for r in rows if r["label"] == "bad" and not r["caught_adaptive"]]
    fps = [r for r in rows if r["label"] == "good" and r["caught_adaptive"]]
    if missed:
        print(f"\nmissed bad runs ({len(missed)}):")
        for r in missed:
            print(f"  {r['duration_sec']}s  {r['sql']}")
    if fps:
        print(f"\nfalse positives ({len(fps)}):")
        for r in fps:
            print(f"  {r['duration_sec']}s  {r['sql']}")

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "split": {"history": len(history), "test": len(test), "frac": 0.70},
        "cold_start_runs": COLD_START_RUNS,
        "anomaly_rule": "above p95 or >3x median of fingerprint history",
        "metrics": {"static_only": m_static, "static_plus_adaptive": m_adaptive},
        "labels": "synthetic — authored anomalies, not human ground truth; "
                  "'bad' runs are 10-100x the data BY CONSTRUCTION",
        "runs": rows,
    }
    dest = ROOT / "eval" / "results" / f"replay_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2))
    print(f"results -> {dest.relative_to(ROOT)}")


# --------------------------------------------------------------------------- #
# Hard variant (F2.2): simulated durations, no warehouse needed.
# --------------------------------------------------------------------------- #

def gen_hard_traffic(seed: int = 7) -> list[dict]:
    """Traffic built to be hard for a per-fingerprint anomaly rule. Every
    query is static-clean, and the row estimate is constant per fingerprint
    (DuckDB estimates range filters at a flat selectivity), so only runtime
    separates good from bad.

    - drift (all good): a report whose runtime grows ~1.5% per run as the
      table grows — legitimate, but test runs sit above the history's p95;
    - noisy (good + 4 bad): lognormal runtimes (sigma 0.7) with 4 labeled-bad
      runs at 3x the median — inside the noise tail, partly invisible;
    - wide_spread (all good): a dashboard that is 10x slower at month-end
      (1 run in 10) — legitimately wide;
    - creep (good, then bad): a gradual regression of 3% per run over the
      last third; runs past 1.5x the original median are labeled bad.
    """
    r = random.Random(seed)
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    runs: list[dict] = []
    for i in range(60):
        ts = base + timedelta(hours=12 * i)
        runs.append({"ts": ts, "fp": "drift", "rows": 500_000, "label": "good",
                     "dur": 0.20 * 1.015 ** i * r.uniform(0.95, 1.05)})
        bad = i in (15, 33, 47, 55)
        runs.append({"ts": ts + timedelta(minutes=5), "fp": "noisy", "rows": 200_000,
                     "label": "bad" if bad else "good",
                     "dur": 0.9 if bad else r.lognormvariate(-1.2, 0.7)})
        runs.append({"ts": ts + timedelta(minutes=10), "fp": "wide_spread", "rows": 1_000_000,
                     "label": "good",
                     "dur": (3.0 if i % 10 == 9 else 0.3) * r.uniform(0.9, 1.1)})
        creep = 0.25 * (1.03 ** max(0, i - 40)) * r.uniform(0.95, 1.05)
        runs.append({"ts": ts + timedelta(minutes=15), "fp": "creep", "rows": 300_000,
                     "label": "bad" if creep > 0.25 * 1.5 else "good", "dur": creep})
    return sorted(runs, key=lambda x: x["ts"])


def run_hard() -> dict:
    traffic = gen_hard_traffic()
    history, test = split_chronological(traffic)
    stats: dict[str, FingerprintStats] = {}
    for rec in history:
        stats.setdefault(rec["fp"], FingerprintStats(rec["fp"])).runs.append((rec["rows"], rec["dur"]))
    rows = []
    for rec in test:
        flag = anomaly_flag(stats.get(rec["fp"]), rec["rows"], rec["dur"])
        rows.append({"fp": rec["fp"], "label": rec["label"], "duration_sec": round(rec["dur"], 3),
                     "static_caught": False, "caught_adaptive": flag is not None})
    per_fp = {}
    for fp in ("drift", "noisy", "wide_spread", "creep"):
        sub = [r for r in rows if r["fp"] == fp]
        per_fp[fp] = {
            "bad": sum(r["label"] == "bad" for r in sub),
            "caught": sum(r["label"] == "bad" and r["caught_adaptive"] for r in sub),
            "good": sum(r["label"] == "good" for r in sub),
            "false_pos": sum(r["label"] == "good" and r["caught_adaptive"] for r in sub),
        }
    bad = [r for r in rows if r["label"] == "bad"]
    good = [r for r in rows if r["label"] == "good"]
    m = {
        "static_only": {"catch_rate": 0.0, "false_positive_rate": 0.0},
        "static_plus_adaptive": {
            "bad_runs": len(bad), "good_runs": len(good),
            "catch_rate": round(sum(r["caught_adaptive"] for r in bad) / len(bad), 3),
            "false_positive_rate": round(sum(r["caught_adaptive"] for r in good) / len(good), 3),
        },
    }
    print(f"hard variant: {len(traffic)} simulated runs ({len(history)} history / {len(test)} test)")
    print(f"\n{'fingerprint':<14}{'bad':>4}{'caught':>8}{'good':>6}{'false-pos':>11}")
    for fp, v in per_fp.items():
        print(f"{fp:<14}{v['bad']:>4}{v['caught']:>8}{v['good']:>6}{v['false_pos']:>11}")
    a = m["static_plus_adaptive"]
    print(f"\nstatic+adaptive: catch {a['catch_rate']}  false-pos {a['false_positive_rate']} "
          f"(static-only catches nothing here by construction: every query is static-clean)")
    out = {
        "ts": datetime.now(timezone.utc).isoformat(), "variant": "hard",
        "durations": "simulated (not executed)", "metrics": m, "per_fingerprint": per_fp,
        "runs": rows,
    }
    dest = ROOT / "eval" / "results" / f"replay_hard_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2))
    print(f"results -> {dest.relative_to(ROOT)}")
    return m


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["standard", "hard"], default="standard")
    args = ap.parse_args()
    if args.variant == "hard":
        run_hard()
    else:
        run_standard()


if __name__ == "__main__":
    main()
