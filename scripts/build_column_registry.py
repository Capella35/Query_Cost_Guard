"""Build or refresh the column registry (data-profiling lookup table).

One scan per table profiles every column (distinct count, NULL count, total
rows) into the `column_registry` table; the Guard looks profiles up there
instead of scanning on the query path. Schedule it (e.g. nightly or monthly):

    python scripts/build_column_registry.py                 # stale + missing
    python scripts/build_column_registry.py --mode missing  # new tables/columns only
    python scripts/build_column_registry.py --mode all      # re-profile everything

Settings (size gate, timeout, max age) come from customer.yaml
`column_registry`. Needs a write connection: run it when no other process
holds the DuckDB file.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cost_guard.column_registry import REGISTRY_TABLE, build_registry, registry_settings  # noqa: E402
from cost_guard.config import load_customer  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "thelook.duckdb"))
    ap.add_argument("--mode", choices=["missing", "stale", "all"], default="stale")
    ap.add_argument("--tables", nargs="*", help="only these tables (default: all base tables)")
    args = ap.parse_args()
    t0 = time.perf_counter()
    rep = build_registry(args.db, args.tables, registry_settings(load_customer()), args.mode)
    print(f"{REGISTRY_TABLE} ({args.mode}): profiled {len(rep.profiled)} table(s), "
          f"{rep.columns} columns in {time.perf_counter() - t0:.1f}s -> {args.db}")
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
