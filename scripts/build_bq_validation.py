#!/usr/bin/env python3
"""Build eval/bq_validation.yaml: every existing eval query, in BigQuery SQL,
labeled for BigQuery. The VALIDATION set: tune on it, rerun it freely.

Sources (all three have been run, so none can be a test set any more):
  eval/eval_queries.yaml (Q01-Q30, DuckDB SQL), eval/holdout_queries.yaml
  (H01-H30, DuckDB SQL), eval/bq_queries.yaml (B01-B30, BigQuery SQL).
DuckDB SQL is adapted with scripts/bq_dryrun_compare.to_bigquery and
dry-run against the TheLook copy (free) for validity and bytes.

Labels follow the BigQuery labeling guide in the output's header (v2: SELECT *
judged on cost). A query keeps its source label unless the guide gives a
different one on BigQuery; every such change is listed in RELABELS with the
measured fact behind it, and kept in the file as `label_from`. The BigQuery
copy holds data to 2026-10 (the public dataset generates dates ahead); the
DuckDB snapshot ends 2024-01, so the same date filter reads more here.

Run:  .venv/bin/python scripts/build_bq_validation.py --project $GCP_PROJECT
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from bq_dryrun_compare import to_bigquery  # noqa: E402

CAP_MIB = 100  # eval/eval_caps_bigquery.yaml, interactive

# id -> (BigQuery label, the measured fact). Bytes are dry runs on the copy.
RELABELS = {
    "Q11": ("block", "reads 207.4 MiB, over the 100 MiB cap (the copy has data to 2026, so 'since 2024' is most of events)"),
    "Q26": ("block", "reads 117.3 MiB, over the 100 MiB cap (same reason as Q11)"),
    "H29": ("block", "reads 226.3 MiB, over the 100 MiB cap (same reason as Q11)"),
    "Q05": ("allow", "filter after a join inside a derived table: BigQuery pushes it down; the pushdown rewrite billed the same bytes (eval/RESULTS_bq_dryrun_full.md)"),
    "Q08": ("allow", "filter after a join inside a derived table: BigQuery pushes it down; the pushdown rewrite billed the same bytes (eval/RESULTS_bq_dryrun_full.md)"),
    "H16": ("allow", "filter after a join inside a derived table: BigQuery pushes it down; the pushdown rewrite billed the same bytes (eval/RESULTS_bq_dryrun_full.md)"),
    "Q09": ("allow", "repeated subquery: the CTE rewrite billed the same bytes on BigQuery (eval/RESULTS_bq_dryrun_full.md); no fewer-bytes equivalent"),
    "Q07": ("allow", "TIMESTAMP_TRUNC(created_at, MONTH) = X prunes on BigQuery: 0.03 MiB; no fewer-bytes equivalent"),
    "H06": ("allow", "CAST(created_at AS DATE) = X prunes on BigQuery: 0.0 MiB; no fewer-bytes equivalent"),
    "H25": ("allow", "TIMESTAMP_TRUNC(created_at, MONTH) = X prunes on BigQuery: 0.2 MiB; no fewer-bytes equivalent"),
    "Q15": ("allow", "HAVING on a group column bills the same as the WHERE form (Q30): 1.84 MiB each, so no waste on BigQuery"),
    "B28": ("allow", "sort with no LIMIT is cheap at this size: live runs 2026-10-17 sorting 284,310 rows took 9.3 slot-s / 1.4 s vs 7.0 slot-s / 0.6 s with LIMIT 1000, same 10 MiB billed"),
}

HEADER = """\
# BigQuery VALIDATION set: every existing eval query (Q, H, B), BigQuery SQL,
# labeled for BigQuery. Built by scripts/build_bq_validation.py — edit the
# script, not this file. Tune on this set and rerun it freely; the held-out
# TEST set is eval/bq_test.yaml (run once).
#
# Provenance: Q01-Q30 (tuning set) and H01-H30 (held-out, run once on DuckDB
# 2026-10-14) were written in DuckDB SQL and adapted with to_bigquery();
# B01-B30 were written for BigQuery (run once 2026-10-16). All 90 have been
# run, so none is held out any more. `label_from` records a label changed for
# BigQuery, with the measured reason. `bytes_mib` is the dry run at build time.
#
# CAPS: eval/eval_caps_bigquery.yaml, interactive — 100 MiB bytes, 5,000,000
# rows for join fan-out, 300 s runtime.
#
# LABELING GUIDE v2 (BigQuery; 2026-10-17). Precedence: block > rewrite > warn > allow.
#   block   - a non-read-only statement; bytes read above the cap (100 MiB);
#             a cartesian product of two tables with more than 1,000 rows
#             each; a join that multiplies rows past 5,000,000 output rows
#             (a low-cardinality or many-to-many non-key join), with or
#             without filters; SUM/AVG of an identifier.
#   rewrite - an EQUIVALENT query (same rows and columns) that bills FEWER
#             bytes and can be produced mechanically. BigQuery prunes through
#             CAST(col AS DATE), DATE(col), TIMESTAMP_TRUNC, DATE_TRUNC(DATE(col)),
#             TIMESTAMP_ADD/SUB, DATE_ADD/SUB, EXTRACT(YEAR|DATE) and
#             FORMAT_TIMESTAMP('%Y-%m-%d', col): NOT rewrite cases. EXTRACT of
#             other parts, FORMAT_TIMESTAMP('%Y-%m', ...) and col + INTERVAL
#             defeat pruning; with an equivalent range, that is a rewrite case.
#   warn    - wasteful, risky or wrong with no equivalent fix, under the cap:
#             SELECT * reading >= 50% of the cap (column count does not
#             matter); no filter on the partition column of a partitioned
#             table (orders, order_items, events, inventory_items); GROUP BY a
#             unique key; a sort of 100k+ rows with no LIMIT; DISTINCT that
#             cannot remove anything; an IN list of more than 100 values;
#             NOT IN over a column that can be NULL.
#   allow   - under the cap and none of the above. A filter after a join
#             inside a derived table, a CTE or subquery used twice, and HAVING
#             on a group column are NOT waste on BigQuery (measured: same
#             bytes), so on their own they are allow.
# role: good = labeled allow; bad = anything else.
# Not testable here: the SELECT * runtime clause (needs 20 logged runs).
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", default="cost_guard_thelook")
    args = ap.parse_args()
    from google.cloud import bigquery

    client = bigquery.Client(project=args.project)

    def mib(sql: str) -> float:
        cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False,
                                      default_dataset=f"{args.project}.{args.dataset}")
        return round(client.query(sql, job_config=cfg).total_bytes_processed / 2**20, 2)

    out = []
    for name, duck in (("eval_queries", True), ("holdout_queries", True), ("bq_queries", False)):
        for q in yaml.safe_load((ROOT / "eval" / f"{name}.yaml").read_text())["queries"]:
            sql = to_bigquery(q["sql"]) if duck else " ".join(q["sql"].split())
            label = q["guard_action_expected"]
            entry = {"id": q["id"], "source": f"eval/{name}.yaml", "pattern": q["pattern"],
                     "description": q["description"], "sql": sql}
            entry["bytes_mib"] = mib(sql)
            if q["id"] in RELABELS:
                new, why = RELABELS[q["id"]]
                assert new != label, q["id"]
                entry["label_from"] = {"label": label, "reason": why}
                label = new
            elif entry["bytes_mib"] > CAP_MIB and label != "block":
                raise SystemExit(f"{q['id']}: {entry['bytes_mib']} MiB is over the cap but "
                                 f"labeled {label}; decide it in RELABELS")
            entry["guard_action_expected"] = label
            entry["role"] = "good" if label == "allow" else "bad"
            out.append(entry)
    path = ROOT / "eval" / "bq_validation.yaml"
    body = yaml.safe_dump({"queries": out}, sort_keys=False, width=110, allow_unicode=True)
    path.write_text(HEADER + "\n" + body)
    print(f"{len(out)} queries -> {path.relative_to(ROOT)} ({len(RELABELS)} relabeled for BigQuery)")


if __name__ == "__main__":
    main()
