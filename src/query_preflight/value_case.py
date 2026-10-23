"""Filter values that match a stored value only when case is ignored.

`status = 'cancelled'` on a column that stores 'Cancelled' matches no row.
The filter value and the column's stored values (the registry's complete
value counts) are compared in lower case; when exactly one stored value
matches, the query is offered with that stored spelling. Not LOWER(col):
that would merge values that differ only by case and stop BigQuery pruning
by the column. Two stored values that differ only by case ('Cancelled',
'CANCELLED') are ambiguous: named, not fixed. Owner decision 2026-10-19.

Covered: `col = 'v'` (either side) and `col [NOT] IN ('v', ...)` on a string column
with complete, fresh value counts. A value that matches nothing even when
case is ignored is left alone (a separate, unbuilt rule).
"""
from typing import Callable, Optional

from sqlglot import exp

from . import sql_ast

STRING_TYPES = ("STRING", "VARCHAR", "TEXT", "CHAR")


def _string_literal(node) -> bool:
    return isinstance(node, exp.Literal) and node.is_string


def _comparisons(sel: exp.Select):
    """(column, literal) pairs from = and IN in this SELECT's WHERE and ON."""
    conds = [sel.args.get("where")] + [j.args.get("on") for j in sel.args.get("joins") or []]
    for cond in conds:
        if cond is None:
            continue
        for node in cond.find_all(exp.EQ, exp.In):
            if node.parent_select is not sel:
                continue  # a subquery's own filter: its SELECT handles it
            if isinstance(node, exp.EQ):
                a, b = node.this, node.expression
                if isinstance(a, exp.Column) and _string_literal(b):
                    yield a, b
                elif isinstance(b, exp.Column) and _string_literal(a):
                    yield b, a
            elif isinstance(node.this, exp.Column) and not node.args.get("query"):
                for lit in node.expressions:
                    if _string_literal(lit):
                        yield node.this, lit


def fix_value_case(tree: exp.Expression, registry, max_age_days: float,
                   has_column: Optional[Callable[[str, str], bool]] = None
                   ) -> tuple[Optional[str], list[str], list[str]]:
    """(corrected SQL or None, notes on corrected values, notes on ambiguous ones)."""
    if registry is None or not hasattr(registry, "complete_value_counts"):
        return None, [], []
    tree = tree.copy()
    ctes = sql_ast.cte_bodies(tree)
    fixed: list[str] = []
    ambiguous: list[str] = []
    for sel in list(tree.find_all(exp.Select)):
        for col, lit in list(_comparisons(sel)):
            table = sql_ast.column_source(sel, col, ctes, has_column)
            entry = registry.lookup(table, col.name) if table else None
            if (entry is None or entry.age_days() > max_age_days
                    or not str(entry.data_type).upper().startswith(STRING_TYPES)):
                continue
            counts = registry.complete_value_counts(table, col.name)
            written = lit.this
            if not counts or written in counts:
                continue
            matches = sorted(v for v in counts if v.lower() == written.lower())
            where = f"{table}.{col.name.lower()}"
            if len(matches) == 1:
                stored = matches[0]
                lit.replace(exp.Literal.string(stored))
                fixed.append(f"{where} = {written!r} matches no stored value; {stored!r} does "
                             f"({counts[stored]:,} rows, column registry) — the result changes")
            elif matches:
                ambiguous.append(f"{where} = {written!r} matches no stored value, and "
                                 + " and ".join(f"{m!r} ({counts[m]:,} rows)" for m in matches)
                                 + " differ only by case (column registry) — say which one")
    if not fixed:
        return None, [], ambiguous
    return tree.sql(dialect=sql_ast.current_dialect()), fixed, ambiguous
