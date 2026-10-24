#!/usr/bin/env python3
"""Cost scorecard: the BigQuery eval sets' verdicts restated in bytes and
MODELED dollars, NEXT TO the verdict scores (never instead of them: wrong
answers and false blocks have no dollar value here).

Verdicts are read from each set's headline run as stored (the run-once sets
from eval/archive/, the tuning sets from their latest eval/results/ file);
nothing is re-decided. Bytes come from BigQuery dry runs made now (free), so
they describe the tables today, not on the run's day; billed = at least
10 MiB per table (runtime.bigquery_billed). Dollars = billed bytes x
customer.yaml cost_model.price_per_tb_usd, MODELED.

Per set:
- bad queries stopped (block): what they would have billed per run — an
  UPPER bound, since the analyst may run a fixed version instead;
- bad queries flagged (warn): billed per run, the analyst decides;
- verified rewrites: original minus rewrite, billed per run (both dry runs);
- missed: bad queries allowed, billed per run;
- false blocks / false warns on good queries: counts (and their bytes — work
  delayed, not money lost); analyst time only with the two ASSUMPTION flags;
- the Guard's own cost: dry runs are free; each rewrite verification bills
  ~30 MiB (measured, DESIGN.md §12), MODELED here per verification.

Run:  .venv/bin/python eval/cost_scorecard.py --project $GCP_PROJECT
      [--analyst-usd-per-hour 60 --minutes-per-false-block 10]   # ASSUMPTIONS, labeled
Writes eval/SCORECARD_cost.md and eval/results/cost_scorecard_<ts>.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from code_version import code_version, describe  # noqa: E402

MiB, TiB = 1024**2, 1024**4
VERIFY_BYTES = 30 * MiB  # one sampled rewrite verification on TheLook (measured, ~30 MiB)

# (suite, results file, what kind of run it is). Run-once sets: their first,
# headline run from eval/archive/; tuning sets: the latest stored run.
SETS = [
    ("bq-validation", None, "tuning"),
    ("bq-pool-validation", None, "tuning"),
    ("bigquery", "eval/archive/bigquery_20261016_050942.json", "held-out, first run"),
    ("bq-test", "eval/archive/bq-test_20261017_221328.json", "held-out, first run"),
    ("bq-pool-test", "eval/archive/bq-pool-test_20261018_043314.json", "held-out, first run"),
    ("bq-owner-test", "eval/archive/bq-owner-test_20261018_065606.json", "held-out, first run"),
]


def latest(suite: str) -> Path:
    files = sorted((ROOT / "eval" / "results").glob(f"{suite}_2*.json"))
    if not files:
        raise SystemExit(f"no stored run of {suite} in eval/results/")
    return files[-1]


def billed(sql: str, wh, cache: dict) -> int:
    """Billed bytes of one run of `sql` (dry run, 10 MiB-per-table minimum)."""
    from query_preflight import sql_ast
    from query_preflight.runtime import bigquery_billed

    if sql not in cache:
        est = wh.estimate(sql).estimated_bytes_scanned or 0
        cache[sql] = bigquery_billed(est, len(sql_ast.referenced_tables(sql_ast.parse_one(sql))))
    return cache[sql]


def score_set(cases: list[dict], sql_of: dict, cost) -> dict:
    """cost(sql) -> billed bytes. One set's scorecard (bytes)."""
    s = {k: 0 for k in ("bad_total", "stopped", "flagged", "fixed", "rewrite_saved", "missed",
                        "false_block_bytes")}
    n = {k: 0 for k in ("bad", "stopped", "flagged", "fixed", "missed", "good", "false_blocks",
                        "false_warns", "rewrites", "verifications")}
    for c in cases:
        b = cost(sql_of[c["id"]])
        got, role = c["got"], c["role"]
        n["verifications"] += (got == "rewrite") + sum(
            "failed verification" in f for f in c.get("findings") or [])
        if got == "rewrite" and c.get("rewritten_sql"):
            n["rewrites"] += 1
            s["rewrite_saved"] += max(0, b - cost(c["rewritten_sql"]))
        if role == "bad":
            n["bad"] += 1
            s["bad_total"] += b
            key = {"block": "stopped", "warn": "flagged", "rewrite": "fixed", "allow": "missed"}[got]
            n[key] += 1
            if key in s:
                s[key] += b
        else:
            n["good"] += 1
            if got == "block":
                n["false_blocks"] += 1
                s["false_block_bytes"] += b
            elif got == "warn":
                n["false_warns"] += 1
    s["guard_verify"] = n["verifications"] * VERIFY_BYTES
    return {"bytes": s, "counts": n}


def main(argv=None) -> int:
    import run_eval
    from query_preflight.backends.bigquery_backend import BigQueryWarehouse
    from query_preflight.config import load_customer

    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", default="cost_guard_thelook")
    ap.add_argument("--analyst-usd-per-hour", type=float, help="ASSUMPTION for false-block time")
    ap.add_argument("--minutes-per-false-block", type=float, help="ASSUMPTION for false-block time")
    args = ap.parse_args(argv)
    version = code_version()
    price = (load_customer().get("cost_model") or {}).get("price_per_tb_usd") or 0
    usd = lambda b: b / TiB * price  # noqa: E731 — MODELED
    wh = BigQueryWarehouse(args.project, args.dataset)
    cache: dict = {}
    out = []
    from query_preflight import sql_ast

    for suite, path, kind in SETS:
        p = ROOT / path if path else latest(suite)
        run = json.loads(p.read_text())
        sql_of = {c["id"]: c["sql"] for c in run_eval.load_suite_cases(suite)}
        with sql_ast.using_dialect("bigquery"):
            sc = score_set(run["cases"], sql_of, lambda q: billed(q, wh, cache))
        m = run["metrics"]
        out.append({"suite": suite, "kind": kind, "run": p.name,
                    # held-out: the headline (labels at the run); tuning: current labels
                    "exact": f"{round(m['exact_accuracy' if kind == 'tuning' else 'exact_accuracy_original_labels'] * m['n'])}/{m['n']}",
                    **sc, "usd": {k: round(usd(v), 6) for k, v in sc["bytes"].items()}})
        print(f"{suite}: {sc['counts']}")

    ts = datetime.now(timezone.utc)
    fmt = lambda b: f"{b / MiB:,.0f} MiB" if b < 1024**3 else f"{b / 1024**3:,.2f} GiB"  # noqa: E731
    money = lambda b: f"${usd(b):.4f}"  # noqa: E731
    md = ["# Cost scorecard: BigQuery eval sets in bytes and MODELED dollars", "",
          f"Generated by `eval/cost_scorecard.py` on {ts:%Y-%m-%d %H:%M} UTC, {describe(version)}. "
          f"Verdicts from each set's stored headline run (nothing re-decided); bytes from BigQuery "
          f"dry runs on `{args.dataset}` made now (free), billed at BigQuery's 10 MiB-per-table "
          f"minimum, **per single run** of each query; dollars at ${price}/TiB, **MODELED**. "
          "Read next to the verdict score, never instead of it: a wrong answer or a false block "
          "has no dollar value here.", "",
          "**Scale.** TheLook is small (its largest table bills ~0.5 GiB), so the dollars are "
          "tiny; the shares carry the meaning. Stopped bytes are an UPPER bound: after a block "
          "the analyst may run a fixed version.", "",
          "| set | run | exact | bad stopped (block) | bad flagged (warn) | bad missed (allow) | "
          "share of bad bytes caught | verified rewrites saved | false blocks / warns on good | "
          "Guard's own cost |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in out:
        b, n = r["bytes"], r["counts"]
        caught = b["stopped"] + b["flagged"] + b["fixed"]  # a verified rewrite catches it too
        share = f"{caught / b['bad_total']:.0%}" if b["bad_total"] else "—"
        md.append(
            f"| {r['suite']} ({r['kind']}) | `{r['run']}` | {r['exact']} | "
            f"{n['stopped']}: {fmt(b['stopped'])}, {money(b['stopped'])} | "
            f"{n['flagged']}: {fmt(b['flagged'])}, {money(b['flagged'])} | "
            f"{n['missed']}: {fmt(b['missed'])}, {money(b['missed'])} | {share} | "
            f"{n['rewrites']}: {fmt(b['rewrite_saved'])}, {money(b['rewrite_saved'])} per run | "
            f"{n['false_blocks']} / {n['false_warns']} of {n['good']} | "
            f"{n['verifications']} verifications ≈ {fmt(b['guard_verify'])}, "
            f"{money(b['guard_verify'])} |")
    if args.analyst_usd_per_hour and args.minutes_per_false_block:
        fb = sum(r["counts"]["false_blocks"] for r in out)
        cost = fb * args.minutes_per_false_block / 60 * args.analyst_usd_per_hour
        md += ["", f"**False-block time (ASSUMPTION, not measured):** {fb} false blocks x "
               f"{args.minutes_per_false_block:g} min x ${args.analyst_usd_per_hour:g}/h = ${cost:.2f}."]
    else:
        md += ["", "**False-block time:** not priced. A false block costs the analyst's time, "
               "which nothing here measures; pass `--analyst-usd-per-hour` and "
               "`--minutes-per-false-block` to show it as a labeled assumption."]
    md += ["", "Columns: *bad stopped / flagged / missed* — count, then billed bytes and MODELED "
           "dollars of one run of those queries. *Share caught* — stopped + flagged + rewritten bad bytes over all "
           "bad bytes. *Verified rewrites saved* — original minus rewrite, billed, one run each. "
           "*Guard's own cost* — dry runs are free; each sampled rewrite verification bills ~30 MiB "
           "(measured on TheLook), counted for offered and failed verifications."]
    (ROOT / "eval" / "SCORECARD_cost.md").write_text("\n".join(md) + "\n")
    dest = ROOT / "eval" / "results" / f"cost_scorecard_{ts:%Y%m%d_%H%M%S}.json"
    dest.write_text(json.dumps({"ts": ts.isoformat(), "code_version": version,
                                "price_per_tb_usd": price, "sets": out}, indent=2))
    print("summary -> eval/SCORECARD_cost.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
