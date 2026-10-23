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
from query_preflight.guard import decide
from eval_config import load_eval_customer  # noqa: E402
from code_version import code_version, describe  # noqa: E402
from query_preflight.thresholds import BaselineStore, FingerprintStats
# The anomaly rule lives in the package (production runs the same code).
from query_preflight.anomaly import (  # noqa: E402,F401
    ANOMALY_MEDIAN_MULT, COLD_START_RUNS, MIN_ABS_DURATION_SEC, anomaly_flag, drift_aware_flag)

VERSION: dict = {}  # code_version(), set in main() before the run

EVAL_CUSTOMER = load_eval_customer()  # caps the replay labels were written under

EXEC_TIMEOUT_SEC = 45  # defensive; authored queries finish in << 1 s

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
        "code_version": VERSION,
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


# Pre-registered 2026-10-14, BEFORE any drift-aware method was written: the
# hard replay's held-out seeds. Methods are developed on HARD_DEV_SEED only and
# evaluated on these once. Same generator and parameters, so they guard
# against fitting one seed's noise, not against fitting the scenario design.
HARD_DEV_SEED = 7
HARD_HELDOUT_SEEDS = tuple(range(101, 111))


def score_hard(seed: int, method: str = "flat") -> tuple[dict, list[dict]]:
    """(per-fingerprint counts, rows) for one seed of the hard traffic.
    method: "flat" (median/p95 of history, the current rule) or "drift"."""
    from query_preflight.thresholds import DriftAwareBaseline

    traffic = gen_hard_traffic(seed)
    history, test = split_chronological(traffic)
    t0 = traffic[0]["ts"]
    hours = lambda rec: (rec["ts"] - t0).total_seconds() / 3600  # noqa: E731
    stats: dict[str, FingerprintStats] = {}
    for rec in history:
        stats.setdefault(rec["fp"], FingerprintStats(rec["fp"])).runs.append((rec["rows"], rec["dur"]))
    baselines = {}
    if method == "drift":
        for fp in stats:
            h = [r for r in history if r["fp"] == fp]
            baselines[fp] = DriftAwareBaseline.fit([hours(r) for r in h], [r["dur"] for r in h])
    rows = []
    for rec in test:
        if method == "drift":
            flag = drift_aware_flag(stats.get(rec["fp"]), baselines.get(rec["fp"]), rec["rows"],
                                    hours(rec), rec["dur"])
        else:
            flag = anomaly_flag(stats.get(rec["fp"]), rec["rows"], rec["dur"])
        rows.append({"fp": rec["fp"], "label": rec["label"], "duration_sec": round(rec["dur"], 3),
                     "caught_adaptive": flag is not None})
    per_fp = {}
    for fp in ("drift", "noisy", "wide_spread", "creep"):
        sub_ = [r for r in rows if r["fp"] == fp]
        per_fp[fp] = {
            "bad": sum(r["label"] == "bad" for r in sub_),
            "caught": sum(r["label"] == "bad" and r["caught_adaptive"] for r in sub_),
            "good": sum(r["label"] == "good" for r in sub_),
            "false_pos": sum(r["label"] == "good" and r["caught_adaptive"] for r in sub_),
        }
    return per_fp, rows


def aggregate(per_fps: list[dict]) -> dict:
    tot = {fp: {k: sum(p[fp][k] for p in per_fps) for k in ("bad", "caught", "good", "false_pos")}
           for fp in per_fps[0]}
    bad = sum(v["bad"] for v in tot.values())
    good = sum(v["good"] for v in tot.values())
    return {"per_fingerprint": tot, "bad_runs": bad, "good_runs": good,
            "catch_rate": round(sum(v["caught"] for v in tot.values()) / bad, 3) if bad else None,
            "false_positive_rate": round(sum(v["false_pos"] for v in tot.values()) / good, 3) if good else None}


# --------------------------------------------------------------------------- #
# Online variant: every run is checked against ALL earlier runs of its shape
# (as runtime.run_query does), then joins the history. Compares option A (keep
# flagged runs in the history) with option B (leave them out). Production code:
# anomaly.check_run.
# --------------------------------------------------------------------------- #

ONLINE_SHAPES = ("drift", "noisy", "wide_spread", "creep", "new_peak")


def gen_online_traffic(seed: int) -> list[dict]:
    """The hard traffic plus one shape for option B's risk: new_peak (all
    good) is a steady 0.3 s report until run 30, then a legitimate 10x
    month-end run every 10 runs (39, 49, 59) — a new normal that starts AFTER
    the cold start. Own RNG, so the four hard shapes are unchanged."""
    runs = gen_hard_traffic(seed)
    r = random.Random(seed * 1000 + 17)
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for i in range(60):
        peak = i >= 30 and i % 10 == 9
        runs.append({"ts": base + timedelta(hours=12 * i, minutes=20), "fp": "new_peak",
                     "rows": 400_000, "label": "good",
                     "dur": (3.0 if peak else 0.3) * r.uniform(0.9, 1.1)})
    return sorted(runs, key=lambda x: x["ts"])


# Pre-registered 2026-10-17 (eval/PREREG_online_anomaly.md), before any run on
# them: fresh seeds — 101-110 were used once by the batch comparison. Option B
# was looked at on HARD_DEV_SEED only.
ONLINE_HELDOUT_SEEDS = tuple(range(201, 221))
ONLINE_RESULTS = ROOT / "eval" / "RESULTS_replay_online.md"


def score_online(seed: int, exclude_flagged: bool) -> tuple[dict, list[dict]]:
    """Per-shape counts over the LAST 30% of runs by time (the batch replays'
    test window), every run checked online against all earlier runs."""
    from query_preflight.anomaly import check_run

    traffic = gen_online_traffic(seed)
    cut = split_chronological(traffic)[1][0]["ts"]
    log: list[dict] = []
    rows = []
    for rec in traffic:
        ts = rec["ts"].isoformat()
        a = check_run(log, rec["fp"], ts, rec["rows"], rec["dur"], exclude_flagged=exclude_flagged)
        log.append({"ts": ts, "fingerprint": rec["fp"], "status": "completed",
                    "duration_sec": rec["dur"], "anomaly_size": rec["rows"],
                    "anomaly": a.reason if a else None})
        if rec["ts"] >= cut:
            rows.append({"fp": rec["fp"], "label": rec["label"], "flag": a is not None})
    per_fp = {}
    for fp in ONLINE_SHAPES:
        sub_ = [r for r in rows if r["fp"] == fp]
        per_fp[fp] = {"bad": sum(r["label"] == "bad" for r in sub_),
                      "caught": sum(r["label"] == "bad" and r["flag"] for r in sub_),
                      "good": sum(r["label"] == "good" for r in sub_),
                      "false_pos": sum(r["label"] == "good" and r["flag"] for r in sub_)}
    return per_fp, rows


def run_online(heldout: bool, rerun: bool = False) -> dict:
    """Options A and B, online. heldout: the pre-registered seeds, run ONCE —
    refused when the results file exists, unless rerun (then labeled)."""
    if heldout and ONLINE_RESULTS.exists() and not rerun:
        raise SystemExit(f"{ONLINE_RESULTS.relative_to(ROOT)} exists: the held-out online seeds "
                         "were run once. --rerun makes it a tuning run (labeled as such).")
    seeds = ONLINE_HELDOUT_SEEDS if heldout else (HARD_DEV_SEED,)
    res = {}
    for name, ex in (("A_keep_flagged", False), ("B_exclude_flagged", True)):
        res[name] = aggregate([score_online(s, ex)[0] for s in seeds])
        print(f"{name}: catch {res[name]['catch_rate']}  false-pos {res[name]['false_positive_rate']}")
    if heldout:
        write_online_summary(res, seeds, rerun)
    return res


def write_online_summary(res: dict, seeds: tuple, rerun: bool) -> None:
    lines = [
        "# Online anomaly replay: option A vs option B",
        "",
        f"Generated by `eval/run_replay.py --variant online-heldout` on "
        f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
        + f", {describe(VERSION)}"
        + (" — RERUN: a tuning run, not the held-out result." if rerun else "."),
        "",
        "Every run is checked against all earlier runs of its shape (as `runtime.run_query` "
        "does) and then joins the history. A keeps flagged runs in the history; B leaves "
        "them out. Scored on the last 30% of runs by time. Simulated runtimes; synthetic "
        "labels written by the same author as the methods. Plan: "
        "`eval/PREREG_online_anomaly.md`.",
        "",
        f"Held-out seeds {seeds[0]}-{seeds[-1]} ({len(seeds)}), pre-registered before any run on them.",
        "",
        "| option | catch | false positives | bad runs | good runs |",
        "|---|---|---|---|---|",
    ]
    for name, a in res.items():
        lines.append(f"| {name} | {a['catch_rate']} | {a['false_positive_rate']} | "
                     f"{a['bad_runs']} | {a['good_runs']} |")
    lines += ["", "## Per shape (bad caught / bad, false positives / good)", "",
              "| shape | A | B |", "|---|---|---|"]
    for fp in ONLINE_SHAPES:
        cells = [f"{res[n]['per_fingerprint'][fp]['caught']}/{res[n]['per_fingerprint'][fp]['bad']}, "
                 f"{res[n]['per_fingerprint'][fp]['false_pos']}/{res[n]['per_fingerprint'][fp]['good']}"
                 for n in res]
        lines.append(f"| {fp} | " + " | ".join(cells) + " |")
    lines += ["", "Shapes: as in RESULTS_replay_hard.md, plus new_peak = a steady report that "
              "gains a legitimate 10x month-end run every 10 runs from run 30 (all good)."]
    ONLINE_RESULTS.write_text("\n".join(lines) + "\n")
    print(f"summary -> {ONLINE_RESULTS.relative_to(ROOT)}")


def run_hard_heldout(method: str = "flat") -> dict:
    """An anomaly-rule method on the pre-registered held-out seeds."""
    agg = aggregate([score_hard(s, method)[0] for s in HARD_HELDOUT_SEEDS])
    print(f"hard variant [{method}], held-out seeds {HARD_HELDOUT_SEEDS[0]}-{HARD_HELDOUT_SEEDS[-1]}: "
          f"catch {agg['catch_rate']}  false-pos {agg['false_positive_rate']}")
    for fp, v in agg["per_fingerprint"].items():
        print(f"  {fp:<14}{v['bad']:>4}{v['caught']:>8}{v['good']:>6}{v['false_pos']:>11}")
    return agg


def write_hard_summary(results: dict) -> None:
    dev = {m: aggregate([score_hard(HARD_DEV_SEED, m)[0]]) for m in results}
    lines = [
        "# Hard replay: flat vs drift-aware baselines",
        "",
        f"Generated by `eval/run_replay.py --variant hard-heldout` on "
        f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC, {describe(VERSION)}. Simulated runtimes "
        "(nothing executed); "
        "4 shapes x 60 runs per seed, 70/30 split by time. Every query is static-clean, so only "
        "the anomaly rule can catch anything.",
        "",
        f"Held-out seeds {HARD_HELDOUT_SEEDS[0]}-{HARD_HELDOUT_SEEDS[-1]} were pre-registered "
        "before the drift-aware method existed; the method was developed on seed "
        f"{HARD_DEV_SEED} only. Same generator and parameters for all seeds: this guards against "
        "fitting one seed's noise, not against fitting the scenario design, which the same "
        "author wrote. Labels are synthetic.",
        "",
        "| method | seeds | catch | false positives |",
        "|---|---|---|---|",
    ]
    for m, agg in results.items():
        lines.append(f"| {m} | held-out ({len(HARD_HELDOUT_SEEDS)}) | {agg['catch_rate']} | "
                     f"{agg['false_positive_rate']} |")
    for m, agg in dev.items():
        lines.append(f"| {m} | dev ({HARD_DEV_SEED}) | {agg['catch_rate']} | {agg['false_positive_rate']} |")
    lines += ["", "## Held-out, per shape (bad caught / bad, false positives / good)", "",
              "| shape | " + " | ".join(results) + " |", "|---|" + "---|" * len(results)]
    for fp in ("drift", "noisy", "wide_spread", "creep"):
        cells = [f"{a['per_fingerprint'][fp]['caught']}/{a['per_fingerprint'][fp]['bad']}, "
                 f"{a['per_fingerprint'][fp]['false_pos']}/{a['per_fingerprint'][fp]['good']}"
                 for a in results.values()]
        lines.append(f"| {fp} | " + " | ".join(cells) + " |")
    lines += ["", "Shapes: drift = runtime grows ~1.5%/run (all good); noisy = lognormal runtimes "
              "with bad runs at 3x the median, inside the noise tail by design; wide_spread = a "
              "legitimate 10x month-end run 1 in 10 (all good); creep = a 3%/run regression over "
              "the last third, bad once past 1.5x the original median."]
    (ROOT / "eval" / "RESULTS_replay_hard.md").write_text("\n".join(lines) + "\n")
    print("summary -> eval/RESULTS_replay_hard.md")


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
        "code_version": VERSION,
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
    ap.add_argument("--variant", choices=["standard", "hard", "hard-heldout", "online",
                                          "online-heldout"], default="standard")
    ap.add_argument("--rerun", action="store_true", help="online-heldout: allow a labeled rerun")
    ap.add_argument("--method", choices=["flat", "drift", "both"], default="both",
                    help="hard-heldout: anomaly-rule baseline to score")
    args = ap.parse_args()
    VERSION.update(code_version())  # before the run: what is measured
    if args.variant == "hard-heldout":
        methods = ["flat", "drift"] if args.method == "both" else [args.method]
        results = {m: run_hard_heldout(m) for m in methods}
        write_hard_summary(results)
    elif args.variant in ("online", "online-heldout"):
        run_online(args.variant == "online-heldout", args.rerun)
    elif args.variant == "hard":
        run_hard()
    else:
        run_standard()


if __name__ == "__main__":
    main()
