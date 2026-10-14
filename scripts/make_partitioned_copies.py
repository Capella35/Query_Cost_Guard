"""Create partitioned copies of the public TheLook tables in your own project.

Public tables are probably not partitioned. The static rule "partitioned tables
need a partition filter" is evaluated against these copies. One-time scan cost.

Fill in the DDL below after scripts/verify_schema.py confirms the real columns,
then run:  python scripts/make_partitioned_copies.py --project $GCP_PROJECT
"""
from __future__ import annotations

import argparse

# TODO: fill in per table after schema verification. Template:
DDL_TEMPLATE = """\
CREATE OR REPLACE TABLE `{project}.{dataset}.{table}_partitioned`
PARTITION BY DATE({partition_column})
AS SELECT * FROM `bigquery-public-data.thelook_ecommerce.{table}`
"""

TABLES: list[tuple[str, str]] = [
    # (table, partition_column) — TODO
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", default="thelook_ecommerce")
    parser.add_argument("--dry_run", action="store_true", help="print DDL without running")
    args = parser.parse_args()
    raise NotImplementedError("Fill in TABLES after schema verification, then run.")


if __name__ == "__main__":
    main()
