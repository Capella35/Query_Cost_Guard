"""Fix hints for blocked queries: what pushed the query over its cap, and what
to change — deterministic, ranked, each fix marked whether it changes the result.

A block used to name the cap and list every finding with the same weight
("SELECT * on 3 tables, no date filter on 3 tables"), which tells neither a
person nor an AI client which change matters. These hints come from the same
inputs as the verdict (table metadata, the estimate, the column registry):

  causes, largest first
    - a table that alone is over the cap and is read in full;
    - a many-to-many join key (the column repeats on BOTH sides, so every
      match pairs with every other: usually a logic error, not only cost);
    - a cartesian product;
  fixes, in the order they most likely help
    - aggregate one side per key before a many-to-many join;
    - a filter on the partition/date column of the largest unfiltered tables,
      with what a 30-day window would read IF rows are spread evenly over
      the registry's min..max (labeled as such);
    - list only the needed columns — on BigQuery it bills fewer bytes, on
      DuckDB it does not change rows processed (said so);
    - a looser workload class, as information only: callers cannot raise their
      own class (the deployment sets it).
  information, before the fixes
    - on BigQuery, a query that already has a LIMIT: LIMIT does not reduce the
      bytes billed (columns and partitions are read in full before rows are
      cut; dry runs on TheLook: SELECT * FROM events 367.2 MiB with or without
      LIMIT 10), so the analyst's attention goes to the date filter and the
      column list instead.

Nothing here rewrites the query: every fix changes the answer, so a person (or
the calling AI) chooses. Hints only appear on blocks; warns already carry
their findings. Not a second opinion on the verdict: no hint changes a decision.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlglot import exp

from . import sql_ast
from .join_estimate import FANOUT_MIN_RATIO

WINDOW_DAYS = 30
# "Unique enough" on one side: distinct >= this share of non-NULL rows. The
# registry's approximate (HyperLogLog) counts measured against exact ones on
# TheLook (47 columns with >= 1,000 values, 2026-10-17): -17% to +16%; truly
# unique keys read as low as 0.831 (products.sku), 0.904 (events.id). So
# approximate counts get a looser bar. A key that truly repeats ~1.2x is then
# not called many-to-many — a missed hint, never a wrong block.
UNIQUE_SHARE = 0.95
UNIQUE_SHARE_APPROX = 0.80


def _hint(kind: str, text: str, changes_result: Optional[bool] = None, **extra) -> dict:
    return {"kind": kind, "text": text, "changes_result": changes_result, **extra}


def _alias_tables(tree, tables: list[str]) -> dict[str, str]:
    out = {}
    for t in tables:
        for a in sql_ast.table_aliases(tree, t):
            out[a] = t
    return out


def _key_edges(tree, alias_tables: dict[str, str]) -> list[tuple[str, str, str, str]]:
    """(table, col, table, col) for every col = col equality between two tables."""
    edges = []
    for eq in tree.find_all(exp.EQ):
        a, b = eq.this, eq.expression
        if not (isinstance(a, exp.Column) and isinstance(b, exp.Column)):
            continue
        ta, tb = alias_tables.get(a.table.lower()), alias_tables.get(b.table.lower())
        if ta and tb and a.table.lower() != b.table.lower():
            edges.append((ta, a.name.lower(), tb, b.name.lower()))
    return edges


def _per_key(entry) -> Optional[float]:
    """Average rows per distinct key value, or None if unknown."""
    if entry is None or not entry.distinct_count:
        return None
    return (entry.total_rows - (entry.null_count or 0)) / entry.distinct_count


def _unique(entry) -> bool:
    nonnull = entry.total_rows - (entry.null_count or 0)
    share = UNIQUE_SHARE_APPROX if entry.is_approximate else UNIQUE_SHARE
    return nonnull > 0 and entry.distinct_count >= share * nonnull


def _days(entry) -> Optional[float]:
    def parse(v):
        try:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00").split("+")[0].strip())
        except ValueError:
            try:
                d = datetime.combine(date.fromisoformat(str(v)[:10]), datetime.min.time())
            except ValueError:
                return None
        return d

    if entry is None or entry.min_value is None or entry.max_value is None:
        return None
    lo, hi = parse(entry.min_value), parse(entry.max_value)
    if lo is None or hi is None or hi <= lo:
        return None
    return (hi - lo).total_seconds() / 86400


def fix_hints(sql: str, verdict, table_md: dict[str, dict], registry, customer: Optional[dict],
              engine: Optional[str], join_ests: Optional[list] = None) -> list[dict]:
    from .config import caps_for

    if "non-read-only statement" in verdict.findings:
        return [_hint("cause", "not a read-only SELECT/WITH query — the Guard only runs reads; "
                      "no change to this statement makes it runnable here")]
    tree = sql_ast.try_parse(sql)
    if tree is None:
        return []
    caps = verdict.caps or {}
    max_rows, max_bytes = caps.get("max_rows_processed"), caps.get("max_bytes_scanned")
    bytes_capped = engine == "bigquery"
    causes: list[dict] = []
    fixes: list[dict] = []

    unfiltered = []  # (size, table, pcol) of tables read without a partition filter
    for t, md in table_md.items():
        pcol = md.get("partition_column")
        use = sql_ast.partition_filter_use(tree, t, pcol) if pcol else None
        filtered = bool(use and (use.bare or use.wrapped))
        size = md.get("num_bytes") if bytes_capped else md.get("num_rows")
        if size and not filtered:
            unfiltered.append((size, t, pcol))
    unfiltered.sort(reverse=True)

    cap, unit = (max_bytes, "bytes") if bytes_capped else (max_rows, "rows")
    for size, t, _ in unfiltered:
        if cap and size > cap:
            causes.append(_hint(
                "cause", f"'{t}' alone is {size:,} {unit}, above the {verdict.workload_class} cap "
                f"of {cap:,}: reading it in full is over the cap whatever else the query does",
                table=t, size=size))

    # Joins: many-to-many keys, and the registry's estimate of the join output.
    alias_tables = _alias_tables(tree, list(table_md))
    m2m = []
    if registry is not None:
        for ta, ca, tb, cb in _key_edges(tree, alias_tables):
            ea, eb = registry.lookup(ta, ca), registry.lookup(tb, cb)
            if ea is None or eb is None or _unique(ea) or _unique(eb):
                continue
            m2m.append((ta, ca, tb, cb, _per_key(ea), _per_key(eb)))
    worst = max((e for e in (join_ests or []) if e.largest_input), key=lambda e: e.rows, default=None)
    for ta, ca, tb, cb, pa, pb in m2m:
        causes.append(_hint(
            "cause", f"join {ta}.{ca} = {tb}.{cb} is many-to-many: {ca} repeats on both sides "
            f"(~{pa:.1f} rows per value in {ta}, ~{pb:.1f} in {tb}), so every {ta} row pairs "
            f"with every matching {tb} row — usually a logic error, not only cost",
            join=f"{ta}.{ca} = {tb}.{cb}"))
        agg_t, agg_c = (ta, ca) if pa >= pb else (tb, cb)  # collapse the side that repeats more
        fixes.append(_hint(
            "fix", f"aggregate {agg_t} per {agg_c} first (a subquery with GROUP BY {agg_c}), "
            "then join — or join on a key that is unique on one side", changes_result=True,
            join=f"{ta}.{ca} = {tb}.{cb}"))
    if worst is not None and worst.fanout >= FANOUT_MIN_RATIO:
        causes.append(_hint(
            "cause", f"the joins multiply rows: estimated output ~{worst.rows:,} rows, "
            f"x{worst.fanout:.1f} the largest input (column registry)", rows=worst.rows))
    if any(f.lower().startswith("cartesian") for f in verdict.findings):
        causes.append(_hint("cause", "cartesian product: tables in one FROM with no join condition"))
        fixes.append(_hint("fix", "add the join condition between the tables", changes_result=True))

    for size, t, pcol in unfiltered:
        if not pcol:
            continue
        entry = registry.lookup(t, pcol) if registry is not None else None
        days = _days(entry)
        impact = None
        if days and days > WINDOW_DAYS:
            impact = round(size * WINDOW_DAYS / days)
            impact_text = (f" — a {WINDOW_DAYS}-day window reads ~{impact:,} of {size:,} {unit} "
                           f"if rows are spread evenly over {days:.0f} days")
        else:
            impact_text = ""
        fixes.append(_hint("fix", f"filter {t}.{pcol} to the dates you need{impact_text}",
                           changes_result=True, table=t, estimated_after=impact))

    if bytes_capped and tree.find(exp.Limit) is not None:
        where = next((f"{t}.{p}" for _, t, p in unfiltered if p), None)
        fixes.insert(0, _hint(
            "info", "LIMIT doesn't reduce bytes billed on BigQuery: the columns and partitions "
            "are read in full before rows are cut. To cut the cost, "
            + (f"filter {where} to the dates you need or " if where else "")
            + "list fewer columns", changes_result=False))

    stars = sql_ast.star_tables(tree)
    if stars:
        effect = ("BigQuery bills per column read, so this cuts bytes" if bytes_capped
                  else "this does not change rows processed (the cap here), only data moved")
        fixes.append(_hint("fix", "list only the columns you need instead of SELECT * — " + effect,
                           changes_result=True))

    # A looser class, for information: the deployment sets the class, not the caller.
    # Named only when the query fits that class's row cap too: a join estimated
    # over it would block there as well (the hint once named 'scheduled' for a
    # ~1.05B-row fan-out under its bytes cap but over its 10M-row cap). Only on
    # a cap block: another class does not change a meaningless-result or
    # cartesian block (it once named 'scheduled' for SUM(order_id)).
    est = verdict.estimated_bytes if bytes_capped else verdict.estimated_rows
    if "hard cap of" not in (verdict.reason or ""):
        est = None
    join_rows = max([verdict.estimated_rows or 0] + ([worst.rows] if worst is not None else []))
    for name in sorted((customer or {}).get("workload_classes") or {}):
        if name == verdict.workload_class or est is None:
            continue
        other = caps_for(customer, name)
        rows_limit = other.get("max_rows_processed")
        if rows_limit and join_rows > rows_limit:
            continue
        limit = other.get("max_bytes_scanned" if bytes_capped else "max_rows_processed")
        if limit and cap and limit > cap and est <= limit:
            fixes.append(_hint(
                "info", f"within the '{name}' class cap ({limit:,} {unit}); if this is a "
                f"{name} job, run it where the deployment assigns '{name}' — callers cannot "
                "raise their own class", changes_result=False))
            break
    return causes + fixes
