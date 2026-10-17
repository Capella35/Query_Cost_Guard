"""Layer 1 static benchmark rules (cold start).

Each rule takes (sql, metadata) and returns a list of findings (empty = clean).
Deterministic: findings, never judgments — the Guard decides. Structure comes
from the sqlglot AST (sql_ast.py), never from regexes over the text.

metadata is a per-table dict: {"table", "engine" ("duckdb" | "bigquery" | None), "num_rows", "column_count", "columns",
"large" (bool), "partition_column", "column_profiles", "table_rows" (row
counts of every referenced table, for the tiny-relation exemption),
"registry" (the column registry or None), "registry_max_age_days"}.
"""
from __future__ import annotations

import re

from .. import sql_ast
from ..column_registry import HIGH_CARDINALITY_RATIO  # noqa: F401 — distinct/rows above = near-unique

MAX_IN_LIST_ITEMS = 100  # judgment call: above this, a join is the better shape
LARGE_TABLE_ROWS = 100_000


def group_by_columns(sql: str, table: str | None = None) -> list[str]:
    """Column names in the GROUP BY clause (lowercased); with `table`, only
    those that can belong to it (see sql_ast.group_by_columns)."""
    tree = sql_ast.try_parse(sql)
    return sql_ast.group_by_columns(tree, table) if tree is not None else []


def require_partition_filter(sql: str, metadata: dict) -> list[str]:
    """Partitioned tables need a partition filter.

    Local path: the 'partition column' is the date column from
    customer.yaml's partitioned_tables — DuckDB's zone maps prune row groups
    on it, so an unfiltered scan reads everything. A filter on the column
    wrapped in a function still counts as "filtered" here; the pruning-killer
    is no_function_on_partition_column's finding.
    """
    pcol = metadata.get("partition_column")
    if not metadata.get("large") or not pcol:
        return []
    use = sql_ast.partition_filter_use(sql_ast.parse_one(sql), metadata["table"], pcol)
    if use.bare or use.wrapped:
        return []
    rows = metadata.get("num_rows")
    rows_s = f"{rows:,}" if isinstance(rows, int) else "?"
    return [
        f"no filter on partition column {pcol} in '{metadata.get('table')}' "
        f"({rows_s} rows) — full scan"
    ]


def no_cross_join_without_condition(sql: str, metadata: dict) -> list[str]:
    """No cartesian product: relations in one FROM that no predicate links
    (explicit CROSS JOIN or comma join), unless provably tiny."""
    return sql_ast.cartesian_findings(
        sql_ast.parse_one(sql), table_rows=metadata.get("table_rows")
    )


# Wrappers BigQuery still prunes through, on a table partitioned by
# DATE(col): each was dry-run against its bare-column equivalent and billed
# the same bytes (2026-10-14, TheLook orders + events; eval/RESULTS_bq_dryrun_*.md).
# 2026-10-16, same method, after the BigQuery eval: EXTRACT(YEAR ...) and
# EXTRACT(DATE ...) prune (= and >=, events + orders); EXTRACT(MONTH / DAY /
# ISOYEAR ...) do not.
# Measured NOT to prune: EXTRACT(DAY/MONTH/ISOYEAR), UNIX_SECONDS, col + INTERVAL,
# FORMAT_TIMESTAMP('%Y-%m', ...), FORMAT_DATE(...), CAST(col AS STRING).
# Anything not listed still warns.
BIGQUERY_PRUNES_THROUGH = frozenset({
    "cast:date", "cast:timestamp",  # CAST(col AS DATE) / AS DATETIME (parsed as TIMESTAMP)
    "date", "timestamp_trunc",      # DATE(col), DATE_TRUNC / TIMESTAMP_TRUNC
    "date_trunc",                   # DATE_TRUNC(DATE(col), ...) as the BigQuery parser names it
    "extract:year", "extract:date",  # EXTRACT(YEAR FROM col), EXTRACT(DATE FROM col)
    "timestamp_add", "timestamp_sub", "date_add", "date_sub",
    "format_timestamp:%Y-%m-%d",
})


def no_function_on_partition_column(sql: str, metadata: dict) -> list[str]:
    """No function wrapped around a partition column in a filter (kills pruning).

    Warehouse-aware: DuckDB skips row groups only for comparisons on the bare
    column, so every wrapper warns there. BigQuery derives partition filters
    through some wrappers (BIGQUERY_PRUNES_THROUGH); on BigQuery those don't
    warn.
    """
    pcol = metadata.get("partition_column")
    if not pcol:
        return []
    use = sql_ast.partition_filter_use(sql_ast.parse_one(sql), metadata["table"], pcol)
    wrapped = use.wrapped
    if metadata.get("engine") == "bigquery":
        wrapped = [w for w, chain in zip(use.wrapped, use.wrapped_chains)
                   if not set(chain) <= BIGQUERY_PRUNES_THROUGH]
    return [
        f"partition column {pcol} of '{metadata.get('table')}' is wrapped in an "
        f"expression in {w!r} — prevents pruning; compare the bare column to a range"
        for w in wrapped
    ]


def filter_before_join(sql: str, metadata: dict) -> list[str]:
    """Filters belong before joins (and before grouping), not after.

    Warehouse-aware: BigQuery pushes a filter written after a join inside a
    derived table down itself — the pushdown rewrite billed the same bytes
    on Q05, Q08 and H16 (eval/RESULTS_bq_dryrun_full.md), and the BigQuery
    eval's B10 was a false warn from it. So on BigQuery only the HAVING
    check remains (not measured there)."""
    findings = sql_ast.filters_after_join(sql_ast.parse_one(sql))
    if metadata.get("engine") == "bigquery":
        findings = [f for f in findings if f.startswith("HAVING")]
    return findings


def repeated_subquery_to_cte(sql: str, metadata: dict) -> list[str]:
    """Repeated identical subqueries should become CTEs."""
    reps = sql_ast.repeated_subqueries(sql_ast.parse_one(sql))
    if reps:
        return [f"identical subquery repeated {reps[0][1]}x — hoist it into a CTE"]
    return []


def no_high_cardinality_groupby(sql: str, metadata: dict) -> list[str]:
    """Warn on GROUP BY over near-unique columns — the profile-aware explainer.

    The warehouse knows the data profile (approx distinct counts); the analyst
    usually doesn't. Grouping by a near-unique key means the aggregation barely
    reduces the data — each group holds ~one row. Profiles come from the
    column registry (built once, refreshed on a schedule), never from a
    per-query scan.
    """
    if not metadata.get("large"):
        return []
    findings = []
    for col in group_by_columns(sql, metadata.get("table")):
        p = metadata.get("column_profiles", {}).get(col)
        if p is None or not p.row_count:
            continue
        if p.approx_distinct / p.row_count >= HIGH_CARDINALITY_RATIO:
            findings.append(
                f"GROUP BY on near-unique column '{col}' in '{metadata.get('table')}': "
                f"~{p.approx_distinct:,} distinct values over {p.row_count:,} rows — "
                f"the aggregation barely reduces the data"
            )
    return findings


IDENTIFIER_DISTINCT_RATIO = 0.95  # distinct / non-NULL rows at or above = near-unique
IDENTIFIER_MIN_ROWS = 1_000  # below this, "every value distinct" proves nothing
_NON_ID_TYPES = ("FLOAT", "DOUBLE", "REAL", "DECIMAL", "NUMERIC")


def _identifier_level(table: str, column: str, metadata: dict) -> tuple[str | None, str | None]:
    """(level, why) for SUM/AVG over `column`: "block", "warn" or None.

    In order: a label a person confirmed decides (identifier or category ->
    block: both are labels, adding or averaging them means nothing; anything
    else -> nothing); a clear id name (id, *_id, *_key, ...) blocks;
    a model label "identifier" not yet confirmed warns; an ambiguous ending
    (_no, _num, _number, _code) warns unless a label says quantity; near-unique
    integer values in the column registry warn. A model label alone never
    blocks (column_kinds.py)."""
    from ..column_kinds import AMBIGUOUS_ID_NAME, STRONG_ID_NAME

    label = (metadata.get("column_kinds") or {}).get(f"{table}.{column}")
    if label and label.get("reviewed"):
        kind = label.get("kind")
        return ("block", f"confirmed {kind}") if kind in ("identifier", "category") else (None, None)
    if STRONG_ID_NAME.search(column):
        return "block", "identifier name"
    if label and label.get("kind") == "identifier":
        return "warn", f"labeled identifier by {label.get('source', 'a model')}, not yet confirmed"
    if label and label.get("kind") == "quantity":
        return None, None
    if AMBIGUOUS_ID_NAME.search(column):
        return "warn", "name ends like an identifier (_no/_num/_code) but may be a count"
    why = _identifier_reason(table, column, metadata)
    return ("warn", why) if why else (None, None)


def _identifier_reason(table: str, column: str, metadata: dict) -> str | None:
    """Near-unique integer values per a fresh column-registry entry, or None."""
    registry = metadata.get("registry")
    entry = registry.lookup(table, column) if registry is not None else None
    if entry is None or entry.age_days() > metadata.get("registry_max_age_days", 35):
        return None
    non_null = entry.total_rows - entry.null_count
    if (non_null >= IDENTIFIER_MIN_ROWS
            and not entry.data_type.upper().startswith(_NON_ID_TYPES)
            and entry.distinct_count >= IDENTIFIER_DISTINCT_RATIO * non_null):
        return f"near-unique values (~{entry.distinct_count:,} distinct in {non_null:,} rows)"
    return None


IDENTIFIER_AGGREGATE = "label aggregate"  # finding prefix the Guard blocks on (ids and confirmed categories)


def no_sum_on_identifier(sql: str, metadata: dict) -> list[str]:
    """No SUM/AVG over an identifier column.

    SUM(customer_id) runs, returns a number, and charts fine — and means
    nothing: an id labels a row, it is not a quantity. Ids are COUNTed.
    A clear id name (`id`, `*_id`, `*_key`, ...) or a kind a person
    confirmed BLOCKS: the finding starts with IDENTIFIER_AGGREGATE and
    carries the query with COUNT(DISTINCT ...) instead. Ambiguous names
    (`*_no`, `*_num`, `*_code`), a model's unconfirmed label and near-unique
    registry values only WARN — each may be a real count or amount, and a
    false block stops work. See _identifier_level.
    """
    table = (metadata.get("table") or "").lower()
    columns = {c.lower() for c in metadata.get("columns", [])}
    if not table:
        return []
    tree = sql_ast.parse_one(sql)
    aliases = sql_ast.table_aliases(tree, table)
    findings = []
    for agg, col in sql_ast.summed_columns(tree):
        name = col.name.lower()
        qualifier = (col.table or "").lower()
        if name not in columns or (qualifier and qualifier not in aliases):
            continue
        level, why = _identifier_level(table, name, metadata)
        if level == "block" and why == "confirmed category":
            findings.append(
                f"{IDENTIFIER_AGGREGATE}: {agg} aggregates category column '{table}.{name}' ({why}) — "
                f"its values are labels (steps, levels, codes); adding or averaging them means "
                f"nothing. Group by it and count (GROUP BY {name} with COUNT(*)), or use "
                f"MIN/MAX({name}) for the first/last value."
            )
        elif level == "block":
            findings.append(
                f"{IDENTIFIER_AGGREGATE}: {agg} aggregates identifier column '{table}.{name}' ({why}) — "
                f"an id labels a row, it is not a quantity; count it: COUNT(DISTINCT {name}) "
                f"(how many different ones) or COUNT({name}) (rows that have one). "
                f"Suggested query: {sql_ast.count_instead(tree, col)}"
            )
        elif level == "warn":
            findings.append(
                f"{agg} may aggregate an identifier column '{table}.{name}' ({why}) — "
                f"if it is an id, count it instead: COUNT(DISTINCT {name}). Confirm the "
                f"column's kind (scripts/classify_columns.py --confirm) to block or clear this"
            )
    return findings


def no_redundant_distinct(sql: str, metadata: dict) -> list[str]:
    """DISTINCT that provably cannot change the result (see
    sql_ast.redundant_distincts). It still costs a hash/sort over every row."""
    return sql_ast.redundant_distincts(sql_ast.parse_one(sql))


def no_long_in_list(sql: str, metadata: dict) -> list[str]:
    """No long explicit IN (...) lists.

    Every value is parsed, planned and compared, and the query text grows
    with the list (BigQuery bills nothing extra but caps query length). A
    list this long is data: load it into a table (or a CTE / UNNEST) and
    join or semi-join against it.
    """
    return [
        f"IN list on {col} has {n:,} explicit values (> {MAX_IN_LIST_ITEMS}) — "
        "put the values in a table or CTE and join against it"
        for col, n in sql_ast.long_in_lists(sql_ast.parse_one(sql), MAX_IN_LIST_ITEMS)
    ]


ALL_RULES = [
    require_partition_filter,
    no_cross_join_without_condition,
    no_function_on_partition_column,
    filter_before_join,
    repeated_subquery_to_cte,
    no_high_cardinality_groupby,
    no_sum_on_identifier,
    no_redundant_distinct,
    no_long_in_list,
]

# Rules about query structure only: run once per query, not once per table,
# and they need no table metadata (cartesian gets table_rows when known).
QUERY_RULES = {
    no_cross_join_without_condition,
    filter_before_join,
    repeated_subquery_to_cte,
    no_redundant_distinct,
    no_long_in_list,
}
