#!/usr/bin/env python3
"""Adaptive-threshold replay on TheLook: DuckDB row estimates vs BigQuery
dry-run bytes, same traffic, same anomaly rule.

The standard replay (run_replay.py) runs on the synthetic warehouse, and its
bytes clause leans on DuckDB's EXPLAIN estimate: a range filter gets a flat
selectivity, raised (never lowered) from the column registry's min/max. On a
partitioned BigQuery table a dry run returns the EXACT bytes after pruning, so
a 7-day and a 2-year window differ by what they really read. This replay asks:
does the anomaly rule's size clause get more valid when its input is exact?

Traffic (TheLook columns, same structure as run_replay.gen_traffic):
- F1: point lookup on users — always cheap, always good (control);
- F2: date-window scan on orders — 7-day windows good, 2-year windows bad
  (1 in 5), same fingerprint (literals differ);
- F3: monthly revenue over order_items, same narrow/wide split;
- F4: cartesian controls — the static rules block them.
Windows stay inside 2019-02..2023-12, where both the local DuckDB export and
the BigQuery copies have data.

Each run gets: the Guard verdict (DuckDB backend), its DuckDB row estimate,
and its BigQuery dry-run bytes on the partitioned copies
(scripts/bq_dryrun_compare.py --create-copies). Split 70/30 by time;
baselines from history; the anomaly rule (run_replay.anomaly_flag, size
clause only — dry runs have no runtime) applied to the test runs once per
size source.

Labels are synthetic and the anomaly is planted (wide windows read far more
data BY CONSTRUCTION). This measures whether the size signal is exact enough
to see it, not production performance. Dry runs only: nothing executes on
BigQuery.

Run:  .venv/bin/python eval/run_replay_bq.py --project $GCP_PROJECT
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "scripts"))

from query_preflight.thresholds import FingerprintStats, fingerprint_query  # noqa: E402
from run_replay import COLD_START_RUNS, anomaly_flag, split_chronological  # noqa: E402

FIRST, LAST = date(2019, 2, 1), date(2023, 12, 1)
WIDE = (date(2021, 1, 1), date(2023, 1, 1))


def gen_traffic(seed: int = 20261014) -> list[dict]:
    rng = random.Random(seed)
    runs: list[dict] = []
    base = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    def add(sql: str, label: str, day: int):
        runs.append({"ts": base + timedelta(days=day, minutes=len(runs) + rng.randint(0, 3)),
                     "sql": sql, "label": label})

    def window(wide: bool) -> tuple[str, str]:
        if wide:
            return WIDE[0].isoformat(), WIDE[1].isoformat()
        start = FIRST + timedelta(days=rng.randint(0, (LAST - FIRST).days - 7))
        return start.isoformat(), (start + timedelta(days=7)).isoformat()

    for i in range(30):
        add(f"SELECT id, first_name, email FROM users WHERE id = {rng.randint(1, 100000)}", "good", i // 10)
    for i in range(30):
        wide = i % 5 == 4
        d1, d2 = window(wide)
        add("SELECT order_id, status, num_of_item FROM orders "
            f"WHERE created_at >= TIMESTAMP '{d1}' AND created_at < TIMESTAMP '{d2}'",
            "bad" if wide else "good", i // 10)
    for i in range(30):
        wide = i % 5 == 2
        d1, d2 = window(wide)
        add("SELECT DATE_TRUNC('month', created_at) AS m, SUM(sale_price) AS revenue FROM order_items "
            f"WHERE created_at >= TIMESTAMP '{d1}' AND created_at < TIMESTAMP '{d2}' "
            "GROUP BY 1 ORDER BY 1 LIMIT 30",
            "bad" if wide else "good", i // 10)
    for i in range(6):
        add("SELECT u.first_name, o.order_id FROM users u, orders o", "bad", i // 2)
    runs.sort(key=lambda r: r["ts"])
    return runs


def measure(runs: list[dict], duck, bq_client, dataset: str, max_bytes: int, customer: dict) -> None:
    """Add verdict, DuckDB row estimate and BigQuery dry-run bytes to each run."""
    from bq_dryrun_compare import dry_run_bytes, to_bigquery
    from query_preflight.guard import decide
    from query_preflight.tracing import Tracer

    for r in runs:
        v = decide(r["sql"], customer=customer, backend=duck, tracer=Tracer())
        r["decision"] = v.decision
        r["duck_rows"] = duck.estimate(r["sql"]).rows_processed
        r["duck_rows_two_way"] = duck.estimate(r["sql"], two_way_ranges=True).rows_processed
        r["bq_bytes"], r["bq_error"] = dry_run_bytes(bq_client, to_bigquery(r["sql"]), dataset, max_bytes)


def evaluate(runs: list[dict], size_key: str) -> dict:
    """Replay with the anomaly rule's size clause fed from `size_key`."""
    history, test = split_chronological(runs)
    stats: dict[str, FingerprintStats] = {}
    for r in history:
        if r["decision"] == "block" or r[size_key] is None:
            continue  # blocked runs never execute; no baseline
        stats.setdefault(fingerprint_query(r["sql"]), FingerprintStats(r["sql"])).runs.append((r[size_key], 0.0))
    rows = []
    for r in test:
        static = r["decision"] in ("warn", "block", "rewrite")
        flag = anomaly_flag(stats.get(fingerprint_query(r["sql"])), r[size_key], None,
                            min_runs=COLD_START_RUNS)
        rows.append({"label": r["label"], "static": static, "flag": flag is not None,
                     "caught": static or flag is not None, "size": r[size_key], "sql": r["sql"][:90]})
    bad = [x for x in rows if x["label"] == "bad"]
    good = [x for x in rows if x["label"] == "good"]
    return {
        "test_runs": len(rows), "bad": len(bad), "good": len(good),
        "catch_static_only": round(sum(x["static"] for x in bad) / len(bad), 3) if bad else None,
        "catch_static_plus_adaptive": round(sum(x["caught"] for x in bad) / len(bad), 3) if bad else None,
        "false_positive_rate": round(sum(x["caught"] for x in good) / len(good), 3) if good else None,
        "missed": [x for x in bad if not x["caught"]],
        "false_positives": [x for x in good if x["caught"]],
    }


def separation(runs: list[dict], key: str) -> list[dict]:
    """Per fingerprint: median size of good vs bad runs, and their ratio."""
    out = []
    for name, marker in (("orders window", "FROM orders"), ("order_items monthly", "FROM order_items")):
        good = sorted(r[key] for r in runs if marker in r["sql"] and r["label"] == "good" and r[key] is not None)
        bad = sorted(r[key] for r in runs if marker in r["sql"] and r["label"] == "bad" and r[key] is not None)
        if good and bad:
            g, b = good[len(good) // 2], bad[len(bad) // 2]
            out.append({"shape": name, "good_median": g, "bad_median": b,
                        "ratio": round(b / g, 1) if g else None})
    return out


def write_summary(duck: dict, duck2: dict, bq: dict, sep: dict, n_runs: int, dataset: str, ts: datetime) -> Path:
    lines = [
        "# Adaptive replay: DuckDB row estimates vs BigQuery dry-run bytes",
        "",
        f"Generated by `eval/run_replay_bq.py` on {ts:%Y-%m-%d %H:%M} UTC. {n_runs} synthetic runs on "
        f"TheLook columns; Guard verdicts on the local DuckDB export; sizes from DuckDB's EXPLAIN "
        f"estimate and from BigQuery dry runs on `{dataset}` (full-history copies partitioned by "
        "`DATE(created_at)`; nothing executed). Split 70/30 by time; the anomaly rule's size "
        f"clause only (dry runs have no runtime); cold start {COLD_START_RUNS} runs.",
        "",
        "**Labels are synthetic and the anomaly is planted**: 2-year windows are bad, 7-day "
        "windows good, same fingerprint. This measures whether the size signal can see a "
        "planted anomaly, not production performance. The test split is small.",
        "",
        "| size signal | test runs (bad/good) | catch, static only | catch, static + adaptive | false positives |",
        "|---|---|---|---|---|",
    ]
    for name, m in (("DuckDB row estimate (raise-only, as the caps use)", duck),
                    ("DuckDB two-way estimate (for the anomaly rule)", duck2),
                    ("BigQuery dry-run bytes", bq)):
        lines.append(f"| {name} | {m['test_runs']} ({m['bad']}/{m['good']}) | {m['catch_static_only']} | "
                     f"{m['catch_static_plus_adaptive']} | {m['false_positive_rate']} |")
    lines += ["", "## Why: how far apart good and bad runs look", "",
              "| size signal | shape | good median | bad median | bad / good |", "|---|---|---|---|---|"]
    for k, label in (("duckdb_rows_estimate", "DuckDB rows (raise-only)"),
                     ("duckdb_rows_two_way", "DuckDB rows (two-way)"),
                     ("bigquery_dry_run_bytes", "BigQuery bytes (exact)")):
        for x in sep[k]:
            lines.append(f"| {label} | {x['shape']} | {x['good_median']:,} | {x['bad_median']:,} | ×{x['ratio']} |")
    lines += ["", "The rule flags a run above its shape's history p95 or 3× its median. DuckDB "
              "estimates a range filter at a flat selectivity, raised (never lowered) from the "
              "column's min/max, so a 2-year window on TheLook's 5-year history looks only ~2× a "
              "7-day one. The two-way estimate also lowers a range filter to the registry's min/max "
              "fraction (assumes evenly spread values; used only to compare runs of one shape, "
              "never for caps). A dry run on a partitioned table returns the bytes after pruning."]
    dest = ROOT / "eval" / "RESULTS_replay_bq.md"
    dest.write_text("\n".join(lines) + "\n")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", default="cost_guard_thelook")
    ap.add_argument("--location", default="US")
    ap.add_argument("--max-bytes-billed", type=int, default=20 * 1024**3)
    ap.add_argument("--db", default=str(ROOT / "data" / "thelook.duckdb"))
    args = ap.parse_args()

    from google.cloud import bigquery

    from query_preflight.backends.duckdb_backend import DuckDBWarehouse
    from eval_config import load_eval_customer

    runs = gen_traffic()
    measure(runs, DuckDBWarehouse(args.db), bigquery.Client(project=args.project, location=args.location),
            f"{args.project}.{args.dataset}", args.max_bytes_billed, load_eval_customer())
    errors = [r for r in runs if r["bq_error"]]
    duck = evaluate(runs, "duck_rows")
    duck2 = evaluate(runs, "duck_rows_two_way")
    bq = evaluate(runs, "bq_bytes")
    sep = {"duckdb_rows_estimate": separation(runs, "duck_rows"),
           "duckdb_rows_two_way": separation(runs, "duck_rows_two_way"),
           "bigquery_dry_run_bytes": separation(runs, "bq_bytes")}

    print(f"traffic: {len(runs)} runs; BigQuery dry-run errors: {len(errors)}")
    print(f"\n{'size signal':<26}{'catch static':>14}{'catch +adaptive':>17}{'false-pos':>11}")
    for name, m in (("DuckDB row estimate", duck), ("DuckDB two-way estimate", duck2),
                    ("BigQuery dry-run bytes", bq)):
        print(f"{name:<26}{m['catch_static_only']!s:>14}{m['catch_static_plus_adaptive']!s:>17}"
              f"{m['false_positive_rate']!s:>11}")
    print("\ngood vs bad run size (median), per shape:")
    for k, rows in sep.items():
        for s in rows:
            print(f"  {k:<24} {s['shape']:<20} good {s['good_median']:>12,}  bad {s['bad_median']:>12,}  x{s['ratio']}")

    ts = datetime.now(timezone.utc)
    out = {"ts": ts.isoformat(), "dataset": f"{args.project}.{args.dataset}",
           "labels": "synthetic; wide windows are bad BY CONSTRUCTION",
           "duckdb_rows_estimate": duck, "duckdb_rows_two_way": duck2,
           "bigquery_dry_run_bytes": bq, "separation": sep,
           "bq_errors": [{"sql": r["sql"][:90], "error": r["bq_error"]} for r in errors],
           "runs": [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()} for r in runs]}
    dest = ROOT / "eval" / "results" / f"replay_bq_{ts:%Y%m%d_%H%M%S}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, default=str))
    print(f"results -> {dest.relative_to(ROOT)}")
    print(f"summary -> {write_summary(duck, duck2, bq, sep, len(runs), f'{args.project}.{args.dataset}', ts).relative_to(ROOT)}")


if __name__ == "__main__":
    main()
