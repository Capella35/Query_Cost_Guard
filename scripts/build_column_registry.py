"""Build or refresh the column registry (data-profiling lookup table).

One scan per table profiles every column (distinct count, NULL count, total
rows, min/max) into a LOCAL registry file next to the warehouse
(data/thelook.duckdb -> data/thelook.registry.duckdb); the Guard looks
profiles up there instead of scanning on the query path. Schedule it (e.g.
nightly or monthly):

    python scripts/build_column_registry.py                 # customer.yaml backend (BigQuery): stale + missing
    python scripts/build_column_registry.py --mode missing  # new tables/columns only
    python scripts/build_column_registry.py --mode all      # re-profile everything
    python scripts/build_column_registry.py --db data/thelook.duckdb   # the local DuckDB copy
    # BigQuery: one billed scan per table (dry-run first, capped), local file
    # data/bigquery/<project>.<dataset>.registry.duckdb
    python scripts/build_column_registry.py --bigquery-project $GCP_PROJECT --dataset cost_guard_thelook

Settings (size gate, timeout, max age) come from customer.yaml
`column_registry`. The warehouse is opened read-only — no write access or
exclusive lock needed; only the registry file is written.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cost_guard.column_registry import (  # noqa: E402
    bigquery_registry_path,
    build_bigquery_registry,
    build_registry,
    default_registry_path,
    registry_settings,
)
from cost_guard.config import load_customer  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="profile this DuckDB file (implies DuckDB; default: "
                    "customer.yaml warehouse.backend, BigQuery)")
    ap.add_argument("--mode", choices=["missing", "stale", "all"], default="stale")
    ap.add_argument("--tables", nargs="*", help="only these tables (default: all base tables)")
    ap.add_argument("--registry", help="registry file (default: <db>.registry.duckdb)")
    ap.add_argument("--bigquery-project", help="BigQuery project (default: GCP_PROJECT)")
    ap.add_argument("--dataset", help="BigQuery dataset (default: customer.yaml)")
    ap.add_argument("--max-bytes-billed", type=int, default=2 * 1024**3,
                    help="BigQuery: per-scan cap; a table over it is reported, not scanned")
    args = ap.parse_args()
    from cost_guard.config import choose_backend, load_dotenv, warehouse_settings

    load_dotenv()
    customer = load_customer()
    settings = registry_settings(customer)
    wcfg = warehouse_settings(customer)
    t0 = time.perf_counter()
    if args.bigquery_project or choose_backend(customer, None, args.db) == "bigquery":
        project, dataset = args.bigquery_project or wcfg["project"], args.dataset or wcfg["dataset"]
        if not project:
            raise SystemExit("BigQuery needs a project: --bigquery-project or GCP_PROJECT")
        registry = args.registry or bigquery_registry_path(project, dataset)
        rep = build_bigquery_registry(project, dataset, args.tables, settings,
                                      args.mode, registry_path=registry,
                                      maximum_bytes_billed=args.max_bytes_billed)
    else:
        db = args.db or wcfg["db_path"]
        registry = args.registry or default_registry_path(db)
        rep = build_registry(db, args.tables, settings, args.mode, registry_path=registry)
    print(f"column registry ({args.mode}): profiled {len(rep.profiled)} table(s), "
          f"{rep.columns} columns in {time.perf_counter() - t0:.1f}s -> {registry}")
    if rep.bytes_processed:
        print(f"  bytes processed by the profiling scans: {rep.bytes_processed:,} "
              f"({rep.bytes_processed / 1024**3:.3f} GiB)")
    if rep.value_counted:
        print(f"  value counts written: {', '.join(rep.value_counted)}")
    if rep.up_to_date:
        print(f"  up to date, not scanned: {', '.join(rep.up_to_date)}")
    if rep.oversized:
        print(f"  over the size gate, NOT scanned: {', '.join(rep.oversized)}")
    if rep.failed:
        print(f"  failed: {'; '.join(rep.failed)}")
    if rep.removed_rows:
        print(f"  removed {rep.removed_rows} rows for dropped tables/columns")


if __name__ == "__main__":
    main()
