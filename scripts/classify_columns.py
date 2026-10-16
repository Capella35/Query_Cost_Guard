#!/usr/bin/env python3
"""Label what columns MEAN, frugally: triage first, a model only for edge cases.

Reads the column registry (build it first: scripts/build_column_registry.py),
settles every column it can without a model (column_kinds.triage), and sends
only the edge cases — numeric columns with no clear name signal, or an
ambiguous ending like _no/_num/_code — to a model in ONE call, at most
--max-columns, from names, types and counts only (never values). Answers are
cached next to the registry (<registry>.column_kinds.json) and asked again
only when a column is new or its type changed.

A model label is a proposal: an "identifier" label only WARNS on SUM/AVG
until a person confirms it here; confirmed labels block like a clear id name.

    python scripts/classify_columns.py --dry-run          # list edge cases; no model call
    python scripts/classify_columns.py                     # label new edge cases (one call)
    python scripts/classify_columns.py --list              # show labels and review state
    python scripts/classify_columns.py --confirm events.sequence_number=quantity
    # BigQuery: add --bigquery-project $GCP_PROJECT [--dataset cost_guard_thelook]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cost_guard.column_kinds import (  # noqa: E402
    classify_edge_cases,
    confirm,
    kinds_path,
    load_kinds,
    triage,
)
from cost_guard.column_registry import (  # noqa: E402
    bigquery_registry_path,
    default_registry_path,
    load_registry,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "thelook.duckdb"))
    ap.add_argument("--bigquery-project")
    ap.add_argument("--dataset", default="cost_guard_thelook")
    ap.add_argument("--max-columns", type=int, default=50,
                    help="at most this many edge cases per model call (cost limit)")
    ap.add_argument("--model", help="model for the edge cases (default: cost_guard.llm.MODEL)")
    ap.add_argument("--dry-run", action="store_true", help="list edge cases; no model call")
    ap.add_argument("--list", action="store_true", help="show labels and their review state")
    ap.add_argument("--confirm", action="append", default=[], metavar="TABLE.COLUMN=KIND",
                    help="a person confirms or corrects a label (repeatable)")
    args = ap.parse_args()

    if args.bigquery_project:
        registry_path, schema = bigquery_registry_path(args.bigquery_project, args.dataset), args.dataset
    else:
        registry_path, schema = default_registry_path(args.db), "main"
    path = kinds_path(registry_path)

    for item in args.confirm:
        column, _, kind = item.partition("=")
        confirm(path, column.strip(), kind.strip())
        print(f"confirmed {column.strip()} = {kind.strip()}")
    if args.confirm:
        return
    if args.list:
        for col, lab in load_kinds(path).items():
            state = f"CONFIRMED by {lab.get('reviewed_by')}" if lab.get("reviewed") else "proposal"
            print(f"{col:45} {lab['kind']:11} {state:24} {lab.get('reason', '')[:70]}")
        return

    registry = load_registry(registry_path, default_schema=schema)
    if registry is None:
        raise SystemExit(f"no column registry at {registry_path} — run scripts/build_column_registry.py")
    entries = registry.entries()
    llm = None
    if not args.dry_run:
        from cost_guard.llm import MODEL, ClaudeLLM

        llm = ClaudeLLM(model=args.model or MODEL)
    rep = classify_edge_cases(entries, path, llm=llm, max_columns=args.max_columns)
    print(f"{len(entries)} columns: {rep.settled} settled without a model, "
          f"{rep.cached} edge cases already labeled, {len(rep.asked)} "
          f"{'would be ' if args.dry_run else ''}sent to the model"
          f"{'' if args.dry_run else f' ({rep.model_calls} call)'}"
          + (f", {len(rep.over_limit)} left for the next run (limit {args.max_columns})"
             if rep.over_limit else ""))
    for col in rep.asked:
        t, c = col.split(".", 1)
        e = next(x for x in entries if x.table_name == t and x.column_name == c)
        print(f"  edge case {col}: {triage(c, e.data_type).why}")
    if not args.dry_run and rep.asked:
        print(f"labels -> {path}; review them with --list, confirm with --confirm")


if __name__ == "__main__":
    main()
