"""Load the TheLook Kaggle CSVs into a local DuckDB file. No cloud needed.

1. Download the CSVs (free Kaggle account) into data/raw/:
     https://www.kaggle.com/datasets/mustafakeser4/looker-ecommerce-bigquery-dataset
   Tables: orders, order_items, users, products, inventory_items, events,
           distribution_centers. (>500 MB total — data/ is gitignored.)
2. python scripts/load_thelook_duckdb.py --data-dir data/raw --db data/thelook.duckdb

Note: the "partition filter" static rule is evaluated against date filters on
created_at — DuckDB's zone maps prune row groups, so no partitioned copies are
needed on the local path.
"""
from __future__ import annotations

import argparse
from pathlib import Path

TABLES = [
    "orders",
    "order_items",
    "users",
    "products",
    "inventory_items",
    "events",
    "distribution_centers",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data/raw")
    parser.add_argument("--db", default="data/thelook.duckdb")
    args = parser.parse_args()

    try:
        import duckdb
    except ImportError:
        raise SystemExit("duckdb is not installed: pip install -r requirements.txt")

    data_dir = Path(args.data_dir)
    missing = [t for t in TABLES if not (data_dir / f"{t}.csv").exists()]
    if missing:
        raise SystemExit(
            f"missing CSVs in {data_dir}: {missing}\n"
            "Download them from the Kaggle link in this file's docstring."
        )

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    for t in TABLES:
        csv = data_dir / f"{t}.csv"
        print(f"loading {t} ...")
        # sample_size=-1: the default sniffer sample sees only gender='F' early in
        # orders.csv and types the column BOOLEAN, then fails on 'M'.
        con.execute(
            f"CREATE OR REPLACE TABLE {t} AS "
            f"SELECT * FROM read_csv('{csv}', header=true, sample_size=-1)"
        )
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {n:,} rows")
    con.close()
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from cost_guard.column_registry import build_registry, registry_settings
    from cost_guard.config import load_customer

    rep = build_registry(str(db_path), settings=registry_settings(load_customer()), mode="all")
    print(f"column registry: {rep.columns} columns profiled"
          + (f"; over size gate, not scanned: {rep.oversized}" if rep.oversized else ""))
    print(f"done -> {db_path}")


if __name__ == "__main__":
    main()
