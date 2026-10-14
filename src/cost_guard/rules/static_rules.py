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

WIDE_COLUMN_THRESHOLD = 8
MAX_IN_LIST_ITEMS = 100  # judgment call: above this, a join is the better shape
LARGE_TABLE_ROWS = 100_000


def group_by_columns(sql: str) -> list[str]:
    """Column names in the GROUP BY clause (unqualified, lowercased)."""
    tree = sql_ast.try_parse(sql)
    return sql_ast.group_by_columns(tree) if tree is not None else []


def no_select_star_on_wide_tables(sql: str, metadata: dict) -> list[str]:
    """No SELECT * on wide tables."""
    cols = metadata.get("column_count") or 0
    table = (metadata.get("table") or "").lower()
    if cols >= WIDE_COLUMN_THRESHOLD and table in sql_ast.star_tables(sql_ast.parse_one(sql)):
        return [
            f"SELECT * on wide table '{metadata.get('table')}' "
            f"({cols} columns) — list only the columns you need"
        ]
    return []


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
# the same bytes (2026-10-14, TheLook events; eval/RESULTS_bq_dryrun.md).
# Measured NOT to prune: EXTRACT, UNIX_SECONDS, col + INTERVAL,
# FORMAT_TIMESTAMP('%Y-%m', ...), FORMAT_DATE(...), CAST(col AS STRING).
# Anything not listed still warns.
BIGQUERY_PRUNES_THROUGH = frozenset({
    "cast:date", "cast:timestamp",  # CAST(col AS DATE) / AS DATETIME (parsed as TIMESTAMP)
    "date", "timestamp_trunc",      # DATE(col), DATE_TRUNC / TIMESTAMP_TRUNC
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
    """Filters belong before joins (and before grouping), not after."""
    return sql_ast.filters_after_join(sql_ast.parse_one(sql))


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
    for col in group_by_columns(sql):
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


IDENTIFIER_NAME = re.compile(r"^id$|(_id|_key|_pk|_uid|_guid|_no|_num|_code)$", re.IGNORECASE)
IDENTIFIER_DISTINCT_RATIO = 0.95  # distinct / non-NULL rows at or above = near-unique
IDENTIFIER_MIN_ROWS = 1_000  # below this, "every value distinct" proves nothing
_NON_ID_TYPES = ("FLOAT", "DOUBLE", "REAL", "DECIMAL", "NUMERIC")


def _identifier_reason(table: str, column: str, metadata: dict) -> str | None:
    """Why `column` looks like an identifier, or None. Name pattern always;
    near-unique integer values when a fresh column-registry entry exists."""
    if IDENTIFIER_NAME.search(column):
        return "identifier name"
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


def no_sum_on_identifier(sql: str, metadata: dict) -> list[str]:
    """No SUM/AVG over an identifier column.

    SUM(customer_id) runs, returns a number, and charts fine — and means
    nothing. Nothing fails loudly, so the Guard says it. A column counts as
    an identifier by name (`id`, `*_id`, `*_key`, `*_code`, ...) or, when
    the column registry has a fresh entry, by near-unique integer values.
    Identifiers are for COUNT(DISTINCT ...), joins and lookups.
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
        why = _identifier_reason(table, name, metadata)
        if why:
            findings.append(
                f"{agg} aggregates identifier column '{table}.{name}' ({why}) — "
                f"the result is a number with no meaning; did you mean "
                f"COUNT(DISTINCT {name})?"
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
    no_select_star_on_wide_tables,
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
