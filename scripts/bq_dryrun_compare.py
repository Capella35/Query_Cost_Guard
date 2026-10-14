#!/usr/bin/env python3
"""Measure what the Guard's rewrites save in BYTES on BigQuery — dry runs only.

Dry runs are free: BigQuery plans the query and reports the exact bytes it
would bill, without running it. On DuckDB most rewrites measure ~1x in
runtime; on BigQuery a date-range rewrite can prune partitions, which shows
up as bytes not billed. This script measures that.

Steps:
  1. (once, --create-copies) Copy TheLook into your project with the four
     created_at tables partitioned by DATE(created_at); the other tables become
     views over the public dataset. This step RUNS queries (a few GB, billed
     or counted against the free tier) under --max-bytes-billed; it prints
     the dry-run bytes first.
  2. Cases: the rewrites the Guard offers on the 30-query set (computed now,
     on the local DuckDB copy — that set is a tuning set), the held-out
     rewrites as recorded by its FIRST run (no new verdicts on the held-out
     set), and two explicit CAST/DATE_TRUNC examples.
  3. Each original and rewrite is adapted to BigQuery SQL (see to_bigquery),
     dry-run against the partitioned copies, and compared.

Output: eval/RESULTS_bq_dryrun.md and eval/results/bq_dryrun_<ts>.json.
Dollars are MODELED from customer.yaml price_per_tb_usd.

Run:
  .venv/bin/python scripts/bq_dryrun_compare.py --project $GCP_PROJECT --plan-copies   # bytes only
  .venv/bin/python scripts/bq_dryrun_compare.py --project $GCP_PROJECT --create-copies
  .venv/bin/python scripts/bq_dryrun_compare.py --project $GCP_PROJECT
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

import sqlglot  # noqa: E402
from dateutil.relativedelta import relativedelta  # noqa: E402
from sqlglot import exp  # noqa: E402

PUBLIC = "bigquery-public-data.thelook_ecommerce"
PARTITIONED = ("orders", "order_items", "events", "inventory_items")
VIEWS = ("users", "products", "distribution_centers")
TB = 1024**4

EXTRA_CASES = [
    ("X1", "SELECT COUNT(*) AS n FROM orders WHERE CAST(created_at AS DATE) = DATE '2023-03-01'"),
    ("X2", "SELECT COUNT(*) AS n FROM events WHERE CAST(created_at AS DATE) = DATE '2023-03-01'"),
]


# --------------------------------------------------------------------------- #
# DuckDB SQL -> BigQuery SQL
# --------------------------------------------------------------------------- #

_UNITS = {"DAY": "days", "WEEK": "weeks", "MONTH": "months", "YEAR": "years"}


def _literal_date(node: exp.Expression):
    """date for CAST('YYYY-MM-DD...' AS DATE/TIMESTAMP/DATETIME) or a typed literal."""
    if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal) and node.this.is_string:
        try:
            return datetime.fromisoformat(node.this.this).date()
        except ValueError:
            return None
    return None


def _fold(node: exp.Expression):
    """Fold literal [+ INTERVAL n unit] into a date; None if not foldable."""
    if isinstance(node, exp.Paren):
        return _fold(node.this)
    d = _literal_date(node)
    if d is not None:
        return d
    if isinstance(node, (exp.Add, exp.Sub)) and isinstance(node.expression, exp.Interval):
        base = _fold(node.this)
        iv = node.expression
        unit = (iv.unit.name if iv.unit else "").upper()
        try:
            n = int(iv.this.this if isinstance(iv.this, exp.Literal) else iv.this.name)
        except (ValueError, AttributeError):
            return None
        if base is None or unit not in _UNITS:
            return None
        delta = relativedelta(**{_UNITS[unit]: n})
        return base + delta if isinstance(node, exp.Add) else base - delta
    return None


def _date_literals(tree: exp.Expression):
    """(Cast node, date) for every CAST('YYYY-MM-DD...' AS DATE/TIMESTAMP)."""
    for c in tree.find_all(exp.Cast):
        d = _literal_date(c)
        if d is not None:
            yield c, d


def months_to_target(sql: str, target: date) -> int:
    """Whole months that move the query's earliest literal date into target's month."""
    dates = [d for _, d in _date_literals(sqlglot.parse_one(sql, read="duckdb"))]
    if not dates:
        return 0
    first = min(dates)
    return (target.year - first.year) * 12 + (target.month - first.month)


def shift_dates(sql: str, months: int) -> str:
    """Move every literal date by whole months (keeps month alignment, so
    DATE_TRUNC('month', ...) cases stay equivalent). Time of day is kept."""
    if not months:
        return sql
    tree = sqlglot.parse_one(sql, read="duckdb")
    for c, _ in list(_date_literals(tree)):
        raw = c.this.this
        moved = datetime.fromisoformat(raw) + relativedelta(months=months)
        text = moved.date().isoformat() if len(raw) <= 10 else moved.isoformat(sep=" ")
        c.set("this", exp.Literal.string(text))
    return tree.sql(dialect="duckdb")


def _ts(d: date) -> exp.Expression:
    # DuckDB TIMESTAMPTZ transpiles to BigQuery TIMESTAMP (DuckDB TIMESTAMP -> DATETIME).
    return exp.Cast(this=exp.Literal.string(d.isoformat()), to=exp.DataType.build("TIMESTAMPTZ"))


def _fold_constant_comparisons(tree: exp.Expression) -> None:
    """Replace comparisons with no column in them (e.g. the DATE_TRUNC rewrite's
    alignment check, TRUNC(literal) = literal) by TRUE/FALSE, evaluated in DuckDB."""
    import duckdb

    con = duckdb.connect()
    for cmp in list(tree.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        if cmp.find(exp.Column) is None:
            value = con.execute("SELECT " + cmp.sql(dialect="duckdb")).fetchone()[0]
            cmp.replace(exp.true() if value else exp.false())


def to_bigquery(sql: str) -> str:
    """Adapt the Guard's DuckDB SQL for TheLook on BigQuery, where the date
    columns are TIMESTAMP and BigQuery will not compare TIMESTAMP with DATE or
    DATETIME. For each comparison whose one side is a bare column: a literal
    date (optionally +/- INTERVAL) on the other side becomes a TIMESTAMP
    literal, folded in Python. Comparisons on CAST(col AS DATE) keep DATE
    literals; comparisons with no column are evaluated to TRUE/FALSE. Then
    transpile duckdb -> bigquery."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    _fold_constant_comparisons(tree)
    for cmp in list(tree.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        for col_side, val_side in (("this", "expression"), ("expression", "this")):
            if isinstance(cmp.args.get(col_side), exp.Column):
                d = _fold(cmp.args[val_side])
                if d is not None:
                    cmp.set(val_side, _ts(d))
    # DATE_TRUNC('unit', ts) = 'literal': BigQuery needs a TIMESTAMP literal too.
    for cmp in list(tree.find_all(exp.EQ)):
        for f_side, val_side in (("this", "expression"), ("expression", "this")):
            if isinstance(cmp.args.get(f_side), (exp.TimestampTrunc, exp.DateTrunc)):
                d = _fold(cmp.args[val_side])
                if d is not None:
                    cmp.set(val_side, _ts(d))
    return sqlglot.transpile(tree.sql(dialect="duckdb"), read="duckdb", write="bigquery")[0]


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #

def thelook_rewrite_cases(db: str) -> list[tuple[str, str, str, str]]:
    """(id, source, original, rewrite) for the 30-query set's offered rewrites."""
    import yaml
    from cost_guard.backends.duckdb_backend import DuckDBWarehouse
    from cost_guard.guard import decide
    from cost_guard.tracing import Tracer
    from eval_config import load_eval_customer

    wh, customer = DuckDBWarehouse(db), load_eval_customer()
    out = []
    for q in yaml.safe_load((ROOT / "eval" / "eval_queries.yaml").read_text())["queries"]:
        v = decide(q["sql"], customer=customer, backend=wh, tracer=Tracer())
        if v.decision == "rewrite":
            out.append((q["id"], v.rewrite_source, q["sql"], v.rewritten_sql))
    for cid, sql in EXTRA_CASES:
        v = decide(sql, customer=customer, backend=wh, tracer=Tracer())
        if v.decision == "rewrite":
            out.append((cid, v.rewrite_source, sql, v.rewritten_sql))
    return out


def holdout_recorded_cases() -> list[tuple[str, str, str, str]]:
    """Rewrites from the held-out set's FIRST run record (no new verdicts)."""
    import yaml

    files = sorted(glob.glob(str(ROOT / "eval" / "results" / "holdout_*.json")))
    if not files:
        return []
    first = json.loads(Path(files[0]).read_text())
    sql = {q["id"]: q["sql"] for q in
           yaml.safe_load((ROOT / "eval" / "holdout_queries.yaml").read_text())["queries"]}
    return [(c["id"], c["rewrite_source"], sql[c["id"]], c["rewritten_sql"])
            for c in first["cases"] if c["got"] == "rewrite"]


# --------------------------------------------------------------------------- #
# BigQuery
# --------------------------------------------------------------------------- #

def dry_run_bytes(client, sql: str, dataset: str | None, max_bytes: int):
    """(bytes, None) or (None, error message). Nothing executes."""
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, maximum_bytes_billed=max_bytes)
    if dataset and "bigquery-public-data" not in sql:
        cfg.default_dataset = dataset
    try:
        return int(client.query(sql, job_config=cfg).total_bytes_processed or 0), None
    except Exception as e:  # noqa: BLE001
        return None, (str(e).splitlines()[0] if str(e) else type(e).__name__)[:200]


def create_copies(client, project: str, dataset: str, location: str, max_bytes: int,
                  plan_only: bool = False) -> None:
    from google.cloud import bigquery

    ddl = [(t, f"CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{t}` "
               f"PARTITION BY DATE(created_at) AS SELECT * FROM `{PUBLIC}.{t}`") for t in PARTITIONED]
    ddl += [(t, f"CREATE VIEW IF NOT EXISTS `{project}.{dataset}.{t}` AS SELECT * FROM `{PUBLIC}.{t}`")
            for t in VIEWS]
    # Bytes come from the SELECT over the public table (a DDL dry run needs the
    # dataset to exist first). Views store nothing and process nothing.
    total = 0
    for t in PARTITIONED:
        b, err = dry_run_bytes(client, f"SELECT * FROM `{PUBLIC}.{t}`", f"{project}.{dataset}", max_bytes)
        meta = client.get_table(f"{PUBLIC}.{t}")
        print(f"  {t:<22} {err or f'{b:,} bytes'} ({meta.num_rows:,} rows)")
        total += b or 0
    print(f"copies: {total:,} bytes would be processed (cap {max_bytes:,})")
    if plan_only:
        return
    ds = bigquery.Dataset(f"{project}.{dataset}")
    ds.location = location
    client.create_dataset(ds, exists_ok=True)
    for t, sql in ddl:
        client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes)).result()
        print(f"  created {t}")


def check_copies(client, project: str, dataset: str) -> list[dict]:
    """Row counts of each copy vs the public table (metadata reads, free). A
    copy far smaller than its source means partitions were dropped — e.g. the
    BigQuery sandbox's 60-day partition expiry on old order dates — and the
    dry-run bytes would overstate the savings. TheLook's public tables grow
    daily, so a small gap is expected."""
    out = []
    for t in PARTITIONED:
        copy = client.get_table(f"{project}.{dataset}.{t}")
        src = client.get_table(f"{PUBLIC}.{t}")
        ratio = copy.num_rows / src.num_rows if src.num_rows else 0.0
        out.append({"table": t, "copy_rows": copy.num_rows, "public_rows": src.num_rows,
                    "ratio": round(ratio, 4), "ok": ratio >= 0.98,
                    "partition_expiration_ms": copy.partition_expiration})
    return out


def pruning_controls(client, dataset: str, max_bytes: int, day: date) -> list[dict]:
    """Which filter forms let BigQuery prune DATE(created_at) partitions: bytes per
    form for one day / one month, against no filter and a form that can't prune."""
    d0, d1 = day.isoformat(), (day + relativedelta(days=1)).isoformat()
    m0 = day.replace(day=1)
    m0s, m1s = m0.isoformat(), (m0 + relativedelta(months=1)).isoformat()
    forms = [
        ("no filter", ""),
        ("CAST(created_at AS DATE) = day", f"WHERE CAST(created_at AS DATE) = DATE '{d0}'"),
        ("DATE(created_at) = day", f"WHERE DATE(created_at) = DATE '{d0}'"),
        ("FORMAT_TIMESTAMP('%Y-%m-%d', created_at) = day",
         f"WHERE FORMAT_TIMESTAMP('%Y-%m-%d', created_at) = '{d0}'"),
        ("range on the bare column (day)",
         f"WHERE created_at >= TIMESTAMP '{d0}' AND created_at < TIMESTAMP '{d1}'"),
        ("TIMESTAMP_TRUNC(created_at, MONTH) = month",
         f"WHERE TIMESTAMP_TRUNC(created_at, MONTH) = TIMESTAMP '{m0s}'"),
        ("range on the bare column (month)",
         f"WHERE created_at >= TIMESTAMP '{m0s}' AND created_at < TIMESTAMP '{m1s}'"),
        ("EXTRACT(DAY FROM created_at) = n", f"WHERE EXTRACT(DAY FROM created_at) = {day.day}"),
    ]
    out = []
    for t, cols in (("orders", "order_id, status"), ("events", "id, uri")):
        for label, where in forms:
            b, err = dry_run_bytes(client, f"SELECT {cols} FROM {t} {where}", dataset, max_bytes)
            out.append({"table": t, "filter": label, "bytes": b, "error": err})
    return out


def compare(client, cases, dataset: str, max_bytes: int, price_per_tb: float) -> list[dict]:
    rows = []
    for cid, source, orig, rew in cases:
        r = {"id": cid, "rewrite_source": source}
        for key, sql in (("original", orig), ("rewrite", rew)):
            try:
                bq_sql = to_bigquery(sql)
            except Exception as e:  # noqa: BLE001
                r[f"{key}_bytes"], r[f"{key}_error"], r[f"{key}_sql"] = None, f"transpile: {e}", None
                continue
            r[f"{key}_sql"] = bq_sql
            r[f"{key}_bytes"], r[f"{key}_error"] = dry_run_bytes(client, bq_sql, dataset, max_bytes)
        ob, rb = r.get("original_bytes"), r.get("rewrite_bytes")
        if ob is not None and rb is not None:
            r["bytes_saved"] = ob - rb
            r["saved_pct"] = round(100 * (ob - rb) / ob, 1) if ob else 0.0
            r["modeled_usd_saved_per_run"] = round((ob - rb) / TB * price_per_tb, 6)
        rows.append(r)
    return rows


def write_summary(rows: list[dict], project_dataset: str, price_per_tb: float, ts: datetime,
                  checks: list[dict] | None = None, shifts: dict | None = None,
                  controls: list[dict] | None = None) -> Path:
    def fmt(b):
        return "error" if b is None else f"{b / 1024**2:,.3f} MiB"

    lines = [
        "# BigQuery dry-run bytes: original vs Guard rewrite",
        "",
        f"Generated by `scripts/bq_dryrun_compare.py` on {ts:%Y-%m-%d %H:%M} UTC against "
        f"`{project_dataset}` (TheLook copies; orders, order_items, events, inventory_items "
        "partitioned by `DATE(created_at)`). **Dry runs only — nothing executed.** "
        f"Dollars are MODELED at {price_per_tb} USD/TB (customer.yaml).",
        "",
        "Queries are the Guard's DuckDB SQL adapted for BigQuery: date literals compared "
        "with a TIMESTAMP column become TIMESTAMP literals and `literal + INTERVAL` bounds "
        "are folded (`to_bigquery`). The Guard's own rewrites are DuckDB SQL; offering them "
        "on BigQuery needs the same adaptation in the rewrite catalog.",
        "",
    ]
    if shifts is not None:
        lines += [
            "**SANDBOX RUN — LOWER BOUND.** The copies were made in a BigQuery sandbox, which "
            "drops partitions older than 60 days, so each copy holds only the most recent "
            "~60 days (row counts below). Each case's dates were moved by whole months into "
            "last month (original and rewrite by the same amount; month alignment kept) so the "
            "filters hit data. A rewrite that ADDS pruning would save less on a 60-day table "
            "than on the full history, so any saving here is a lower bound; a snapshot of the "
            "run date.",
            "",
        ]
    measured = [r for r in rows if "saved_pct" in r]
    if measured:
        gains = [r for r in measured if r["saved_pct"] > 0]
        lines += [f"**Result:** {len(gains)} of {len(measured)} rewrites bill fewer bytes than "
                  "their original."
                  + ("" if gains else " Where the original already prunes partitions (see the "
                     "controls), the table's length does not change this: original and rewrite "
                     "prune the same partitions."), ""]
    lines += [
        "| case | fixer | original | rewrite | saved | modeled USD saved / run |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        saved = f"{r['saved_pct']}%" if "saved_pct" in r else "—"
        usd = r.get("modeled_usd_saved_per_run", "—")
        shift = f" (dates +{shifts[r['id']]} mo)" if shifts and shifts.get(r["id"]) else ""
        lines.append(f"| {r['id']}{shift} | {r['rewrite_source']} | {fmt(r.get('original_bytes'))} | "
                     f"{fmt(r.get('rewrite_bytes'))} | {saved} | {usd} |")
    errs = [r for r in rows if r.get("original_error") or r.get("rewrite_error")]
    if errs:
        lines += ["", "## Errors", ""]
        for r in errs:
            for k in ("original", "rewrite"):
                if r.get(f"{k}_error"):
                    lines.append(f"- **{r['id']}** {k}: {r[f'{k}_error']}")
    if controls:
        lines += ["", "## Which filter forms prune partitions (controls)", "",
                  "Same query, different filter on `created_at`; dry-run bytes.", "",
                  "| table | filter | bytes |", "|---|---|---|"]
        lines += [f"| {c['table']} | `{c['filter']}` | {fmt(c['bytes'])} |" for c in controls]
    if checks:
        lines += ["", "## Copies (row counts vs the public tables)", "",
                  "| table | copy rows | public rows | ratio |", "|---|---|---|---|"]
        lines += [f"| {c['table']} | {c['copy_rows']:,} | {c['public_rows']:,} | {c['ratio']} |" for c in checks]
    lines += ["", "Cases: the 30-query set's rewrites (a tuning set), the held-out set's rewrites "
              "as recorded by its first run, and X1/X2 (`CAST(created_at AS DATE) = X`)."]
    dest = ROOT / "eval" / "RESULTS_bq_dryrun.md"
    dest.write_text("\n".join(lines) + "\n")
    return dest


def main() -> None:
    from cost_guard.config import load_customer

    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", default="cost_guard_thelook")
    ap.add_argument("--location", default="US")
    ap.add_argument("--max-bytes-billed", type=int, default=20 * 1024**3,
                    help="cap for every job, including the copy step (default 20 GiB)")
    ap.add_argument("--create-copies", action="store_true",
                    help="create the partitioned copies first (runs queries; see the dry-run bytes it prints)")
    ap.add_argument("--plan-copies", action="store_true",
                    help="only print the bytes the copy step would process, then exit")
    ap.add_argument("--recent-window", action="store_true",
                    help="sandbox copies (60-day partition expiry): move case dates into last "
                         "month and report savings as a lower bound")
    ap.add_argument("--db", default=str(ROOT / "data" / "thelook.duckdb"))
    args = ap.parse_args()

    from google.cloud import bigquery

    client = bigquery.Client(project=args.project, location=args.location)
    if args.plan_copies or args.create_copies:
        create_copies(client, args.project, args.dataset, args.location, args.max_bytes_billed,
                      plan_only=args.plan_copies)
        if args.plan_copies:
            return
    checks = check_copies(client, args.project, args.dataset)
    for c in checks:
        print(f"  copy {c['table']:<16} {c['copy_rows']:,} / {c['public_rows']:,} rows "
              f"({'ok' if c['ok'] else 'TRUNCATED'}; partition expiry {c['partition_expiration_ms']})")
    shifts: dict[str, int] = {}
    if not all(c["ok"] for c in checks) and not args.recent_window:
        raise SystemExit("copies are missing rows (partition expiry?) — the byte comparison would "
                         "overstate savings. Link billing / remove the expiry and recreate the copies.")
    price = (load_customer().get("cost_model") or {}).get("price_per_tb_usd") or 0.0
    cases = thelook_rewrite_cases(args.db) + holdout_recorded_cases()
    if args.recent_window:
        # Sandbox copies keep ~60 days: move each case (original and rewrite by
        # the same amount) into last month, which always lies inside the window.
        today = datetime.now(timezone.utc).date()
        target = (today.replace(day=1) - relativedelta(months=1))
        moved = []
        for cid, src, o, r in cases:
            k = months_to_target(o, target)
            shifts[cid] = k
            moved.append((cid, src, shift_dates(o, k), shift_dates(r, k)))
        cases = moved
    ds = f"{args.project}.{args.dataset}"
    rows = compare(client, cases, ds, args.max_bytes_billed, price)
    ctrl_day = (datetime.now(timezone.utc).date().replace(day=1) - relativedelta(months=1)).replace(day=24)
    controls = pruning_controls(client, ds, args.max_bytes_billed, ctrl_day)
    ts = datetime.now(timezone.utc)
    out = ROOT / "eval" / "results" / f"bq_dryrun_{ts:%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"ts": ts.isoformat(), "dataset": ds, "copies": checks,
                               "recent_window": args.recent_window, "month_shifts": shifts,
                               "rows": rows, "pruning_controls": controls}, indent=2))
    for r in rows:
        print(f"{r['id']:<5} {r.get('original_bytes')!s:>14} -> {r.get('rewrite_bytes')!s:>14}  "
              f"{r.get('saved_pct', '—')}%  {r.get('original_error') or r.get('rewrite_error') or ''}")
    print(f"summary -> {write_summary(rows, ds, price, ts, checks, shifts if args.recent_window else None, controls).relative_to(ROOT)}\nresults -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
