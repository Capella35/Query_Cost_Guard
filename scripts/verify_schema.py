"""Build step 1: verify the live TheLook schema before trusting eval_queries.yaml.

Lists tables + columns in bigquery-public-data.thelook_ecommerce and checks that
every table referenced in the eval set exists. Table/column names in the eval file
were written from memory — this script is the check.

Usage:
    python scripts/verify_schema.py --project $GCP_PROJECT
"""
from __future__ import annotations

import argparse
import re
import sys

import yaml

TABLE_REF = re.compile(r"thelook_ecommerce\.(\w+)", re.IGNORECASE)


def live_tables(project: str) -> dict[str, list[str]]:
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    dataset = client.get_dataset("bigquery-public-data.thelook_ecommerce")
    out: dict[str, list[str]] = {}
    for table in client.list_tables(dataset):
        t = client.get_table(table)
        out[table.table_id] = [s.name for s in t.schema]
    return out


def eval_table_refs(eval_path: str) -> set[str]:
    with open(eval_path) as f:
        data = yaml.safe_load(f)
    refs: set[str] = set()
    for q in data["queries"]:
        sql = q.get("sql") or ""
        refs.update(TABLE_REF.findall(sql))
    return refs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="your GCP project")
    parser.add_argument("--eval", default="eval/eval_queries.yaml")
    args = parser.parse_args()

    live = live_tables(args.project)
    print(f"Live thelook_ecommerce tables ({len(live)}):")
    for name, cols in sorted(live.items()):
        print(f"  {name}: {len(cols)} columns")

    refs = eval_table_refs(args.eval)
    if not refs:
        print("\nNo table references found in eval SQL yet (sql fields are TODO).")
        return
    missing = refs - set(live)
    if missing:
        print(f"\nMISSING from live schema: {sorted(missing)}")
        sys.exit(1)
    print("\nAll eval table references exist in the live schema.")


if __name__ == "__main__":
    main()
