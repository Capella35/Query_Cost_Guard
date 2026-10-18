#!/usr/bin/env python3
"""Build eval/bq_validation.yaml: the BigQuery-native eval queries, labeled
for BigQuery. The VALIDATION set: tune on it, rerun it freely.

Sources (both written in BigQuery SQL, both already run, so neither can be a
test set any more):
  eval/bq_queries.yaml (B01-B30, run once 2026-10-16) and eval/bq_test.yaml
  (T01-T40, pre-registered ddf0030, run once 2026-10-17; fixes followed its
  misses, so it became a tuning set).
The DuckDB-era queries (eval/eval_queries.yaml Q01-Q30, eval/holdout_queries.yaml
H01-H30) left the BigQuery evals on 2026-10-17 (owner's call: BigQuery-native
only); their files stay as DuckDB history. Each query is dry-run against the
TheLook copy (free) for validity and bytes.

Labels follow the BigQuery labeling guide in the output's header. A source's
own recorded `label_change` is kept. A label set for BigQuery when the set is
built goes in RELABELS (kept as `label_from`); a label changed after a run goes
in POST_RUN_RELABELS (kept as `label_change`, so both scores are reported).

Run:  .venv/bin/python scripts/build_bq_validation.py --project $GCP_PROJECT
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

CAP_MIB = 100  # eval/eval_caps_bigquery.yaml, interactive

# id -> (BigQuery label, the measured fact), set when the set is built. Empty
# since the DuckDB-era queries left (their 11 entries are in git history).
RELABELS: dict[str, tuple[str, str]] = {}

# Label changes made AFTER this set was run (2026-10-17 first run): recorded
# as `label_change` (from / to / date / reason, from_role when the role
# changes), so run_eval.py scores both the original and the current labels.
# id -> (new label, date, reason)
POST_RUN_RELABELS = {
    "B28": ("allow", "2026-10-17", "owner's choice after the first validation run: sort with no LIMIT is cheap at this size; live runs 2026-10-17 sorting 284,310 rows took 9.3 slot-s / 1.4 s vs 7.0 slot-s / 0.6 s with LIMIT 1000, same 10 MiB billed"),
    "B22": ("warn", "2026-10-17", "owner's policy 2026-10-17: a rewrite must BILL fewer bytes (BigQuery bills at least 10 MiB per table); dry run 4.155 -> 0.034 MiB read, 10 -> 10 MiB billed, so no rewrite; the unprunable wrapper still warns"),
    "B23": ("warn", "2026-10-17", "owner's policy 2026-10-17: a rewrite must BILL fewer bytes (BigQuery bills at least 10 MiB per table); dry run 0.958 -> 0.713 MiB read, 10 -> 10 MiB billed, so no rewrite; the unprunable wrapper still warns"),
    "T15": ("warn", "2026-10-17", "owner's policy 2026-10-17: a rewrite must BILL fewer bytes; dry run 0.721 -> 0.058 MiB read, 10 -> 10 MiB billed, so no rewrite; the EXTRACT(MONTH) wrapper still warns"),
    "T24": ("allow", "2026-10-17", "owner's choice (as B28): a sort with no LIMIT is cheap at this size on BigQuery; B28 measured live 9.3 vs 7.0 slot-s, same bytes billed; 37 MiB read, under the cap"),
}
ROLE = {"allow": "good"}

HEADER = """\
# BigQuery VALIDATION set: the BigQuery-native eval queries (B, T), labeled for
# BigQuery. Built by scripts/build_bq_validation.py — edit the script, not this
# file. Tune on this set and rerun it freely. There is no untouched test set
# now: a fresh pool is to be written, pre-registered and split at random.
#
# Provenance: B01-B30 were written for BigQuery (run once 2026-10-16); T01-T40
# were written for BigQuery as the test set (pre-registered ddf0030, run once
# 2026-10-17: 35/40), then fixes followed its misses. All 70 have been run, so
# none is held out. The DuckDB-era Q and H queries left on 2026-10-17. `label_from` records a label changed for
# BigQuery when this set was built, with the measured reason. `label_change`
# records a label changed AFTER this set was run; run_eval.py scores both the
# original and the current labels. `bytes_mib` is the dry run at build time.
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
#             EXCEPTION (Guard, 2026-10-17): the fan-out estimate applies
#             date-partition filters and `col = value` / `col IN (values)`;
#             under any other filter (a range, LIKE, a join on an expression)
#             it is only traced, so such a fan-out is a known miss.
#   rewrite - an EQUIVALENT query (same rows and columns) that BILLS fewer
#             bytes (billed = max(bytes read, 10 MiB per table), 2026-10-17)
#             and can be produced mechanically. BigQuery prunes through
#             CAST(col AS DATE), DATE(col), TIMESTAMP_TRUNC, DATE_TRUNC(DATE(col)),
#             TIMESTAMP_ADD/SUB, DATE_ADD/SUB, EXTRACT(YEAR|DATE) and
#             FORMAT_TIMESTAMP('%Y-%m-%d', col): NOT rewrite cases. EXTRACT of
#             other parts, FORMAT_TIMESTAMP('%Y-%m', ...) and col + INTERVAL
#             defeat pruning; with an equivalent range, that is a rewrite case.
#   warn    - wasteful, risky or wrong with no equivalent fix, under the cap:
#             SELECT * reading >= 50% of the cap (column count does not
#             matter); no filter on the partition column of a partitioned
#             table (orders, order_items, events, inventory_items); GROUP BY a
#             unique key; DISTINCT that
#             cannot remove anything; an IN list of more than 100 values;
#             NOT IN over a column that can be NULL.
#   allow   - under the cap and none of the above. A filter after a join
#             inside a derived table, a CTE or subquery used twice, and HAVING
#             on a group column are NOT waste on BigQuery (measured: same
#             bytes), so on their own they are allow. So is a sort with no
#             LIMIT under the cap (owner's choice 2026-10-17, B28 measured).
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
    for name in ("bq_queries", "bq_test"):
        for q in yaml.safe_load((ROOT / "eval" / f"{name}.yaml").read_text())["queries"]:
            sql = " ".join(q["sql"].split())
            label = q["guard_action_expected"]
            entry = {"id": q["id"], "source": f"eval/{name}.yaml", "pattern": q["pattern"],
                     "description": q["description"], "sql": sql}
            if q.get("label_change"):  # recorded in the source: kept
                assert q["id"] not in POST_RUN_RELABELS, q["id"]
                entry["label_change"] = q["label_change"]
            entry["bytes_mib"] = mib(sql)
            if q["id"] in RELABELS:
                new, why = RELABELS[q["id"]]
                assert new != label, q["id"]
                entry["label_from"] = {"label": label, "reason": why}
                label = new
            elif entry["bytes_mib"] > CAP_MIB and label != "block":
                raise SystemExit(f"{q['id']}: {entry['bytes_mib']} MiB is over the cap but "
                                 f"labeled {label}; decide it in RELABELS")
            role = ROLE.get(label, "bad")
            if q["id"] in POST_RUN_RELABELS:
                new, date, why = POST_RUN_RELABELS[q["id"]]
                assert new != label, q["id"]
                change = {"from": label, "to": new, "date": date, "reason": why}
                new_role = ROLE.get(new, "bad")
                if new_role != role:
                    change["from_role"] = role
                entry["label_change"] = change
                label, role = new, new_role
            entry["guard_action_expected"] = label
            entry["role"] = role
            out.append(entry)
    path = ROOT / "eval" / "bq_validation.yaml"
    body = yaml.safe_dump({"queries": out}, sort_keys=False, width=110, allow_unicode=True)
    path.write_text(HEADER + "\n" + body)
    print(f"{len(out)} queries -> {path.relative_to(ROOT)} ({len(RELABELS)} relabeled for BigQuery, "
          f"{len(POST_RUN_RELABELS)} label changes after a run)")


if __name__ == "__main__":
    main()
