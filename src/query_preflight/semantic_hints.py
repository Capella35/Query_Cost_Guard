"""Semantic hints: what the data means (grain, keys, valid joins), next to
the column registry's counts. DRAFT 2026-10-24: not yet called by
guard.decide() — the two rules below wait for the owner's approval.

The file (semantic_hints.yaml) is read once, never scanned per query. Two
rules read it, both aimed at wrong answers (policy priority 1), both
deterministic:

- check_join_keys: two hinted tables joined on a column pair that is not a
  declared join (order_items.user_id = orders.user_id: every item of a
  customer meets every order of that customer; measured 2.04x the rows on
  TheLook). Fine when the declared pair is also in the condition.
- check_fanout_sums: SUM/AVG over a column of the "one" side of a declared
  many-to-one join, with the many side joined in the same SELECT: the value
  repeats once per many-side row, so the total is multiplied
  (SUM(sale_price * num_of_item) over order_items x orders: $20.6M instead of
  $10.8M on TheLook, 1.9x). A pre-aggregated subquery is its own SELECT and
  is not flagged.

Both return findings (strings); they would warn, never block: a query can be
right for a reason the hints don't know.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from sqlglot import exp

from . import sql_ast

ColumnRef = tuple[str, str]  # (table, column), lower case


@dataclass
class Hints:
    tables: dict[str, dict] = field(default_factory=dict)  # table -> {grain, key}
    # (many side, one side): the one side's column is its table's key
    joins: list[tuple[ColumnRef, ColumnRef]] = field(default_factory=list)
    columns: dict[ColumnRef, dict] = field(default_factory=dict)  # (table, column) -> {note}

    def declared(self, a: str, b: str) -> list[tuple[ColumnRef, ColumnRef]]:
        """Declared joins between tables a and b, either direction."""
        return [j for j in self.joins if {j[0][0], j[1][0]} == {a, b}]

    def is_declared(self, x: ColumnRef, y: ColumnRef) -> bool:
        return (x, y) in self.joins or (y, x) in self.joins


def _ref(text: str) -> ColumnRef:
    table, _, column = text.strip().lower().rpartition(".")
    if not table or not column:
        raise ValueError(f"expected table.column, got {text!r}")
    return table, column


def load_hints(path: str | Path) -> Hints:
    """Parse and validate the file: every join's right side must be its
    table's declared key (that is what makes it the "one" side)."""
    import yaml

    raw = yaml.safe_load(Path(path).read_text()) or {}
    h = Hints(tables={t.lower(): v for t, v in (raw.get("tables") or {}).items()})
    for spec in raw.get("joins") or []:
        left, sep, right = spec.partition("=")
        if not sep:
            raise ValueError(f"join {spec!r}: expected many.col = one.col")
        many, one = _ref(left), _ref(right)
        key = (h.tables.get(one[0]) or {}).get("key", "").lower()
        if one[1] != key:
            raise ValueError(f"join {spec!r}: {one[0]}.{one[1]} is not {one[0]}'s declared key "
                             f"({key or 'none'})")
        h.joins.append((many, one))
    h.columns = {_ref(c): v or {} for c, v in (raw.get("columns") or {}).items()}
    return h


def _scope_tables(sel: exp.Select, ctes: dict) -> dict[str, str]:
    """alias -> physical table for the tables read directly by this SELECT."""
    out = {}
    for rel, _ in sql_ast._relations(sel):
        if isinstance(rel, exp.Table) and rel.name.lower() not in ctes:
            out[(rel.alias_or_name or rel.name).lower()] = rel.name.lower()
    return out


def _resolve(col: exp.Column, scope: dict[str, str],
             has_column: Optional[Callable[[str, str], bool]]) -> Optional[ColumnRef]:
    """The (table, column) a column reads in this scope, or None if unsure."""
    name = col.name.lower()
    if col.table:
        t = scope.get(col.table.lower())
        return (t, name) if t else None
    tables = set(scope.values())
    if len(tables) == 1:
        return next(iter(tables)), name
    if has_column is not None:
        owners = [t for t in tables if has_column(t, name)]
        if len(owners) == 1:
            return owners[0], name
    return None


def _selects(tree: exp.Expression):
    return [s for s in tree.find_all(exp.Select)]


def check_join_keys(tree: exp.Expression, hints: Hints,
                    has_column: Optional[Callable[[str, str], bool]] = None) -> list[str]:
    """Hinted tables joined only on undeclared column pairs."""
    ctes = sql_ast.cte_bodies(tree)
    findings = []
    for sel in _selects(tree):
        scope = _scope_tables(sel, ctes)
        conds = [c for _, j in sql_ast._relations(sel) if j is not None
                 for c in sql_ast.conjuncts(j.args.get("on"))]
        conds += sql_ast.conjuncts(sel.args.get("where"))  # comma joins
        pairs: dict[frozenset, list[tuple[ColumnRef, ColumnRef]]] = {}
        for c in conds:
            if not (isinstance(c, exp.EQ) and isinstance(c.this, exp.Column)
                    and isinstance(c.expression, exp.Column)):
                continue
            x, y = _resolve(c.this, scope, has_column), _resolve(c.expression, scope, has_column)
            if x is None or y is None or x[0] == y[0]:
                continue
            pairs.setdefault(frozenset((x[0], y[0])), []).append((x, y))
        for tables, eqs in pairs.items():
            a, b = sorted(tables)
            declared = hints.declared(a, b)
            if not declared or any(hints.is_declared(x, y) for x, y in eqs):
                continue
            used = ", ".join(f"{x[0]}.{x[1]} = {y[0]}.{y[1]}" for x, y in eqs)
            want = " or ".join(f"{m[0]}.{m[1]} = {o[0]}.{o[1]}" for m, o in declared)
            findings.append(
                f"join key not declared: {a} and {b} are joined on {used}; the declared key is "
                f"{want} (semantic hints) — another column can match unrelated rows and "
                f"inflate totals")
    return findings


def check_fanout_sums(tree: exp.Expression, hints: Hints,
                      has_column: Optional[Callable[[str, str], bool]] = None) -> list[str]:
    """SUM/AVG over a "one"-side column with its many side joined alongside."""
    ctes = sql_ast.cte_bodies(tree)
    findings, seen = [], set()
    for sel in _selects(tree):
        scope = _scope_tables(sel, ctes)
        tables = set(scope.values())
        fanned = {one[0]: many[0] for many, one in hints.joins
                  if many[0] in tables and one[0] in tables}
        if not fanned:
            continue
        for agg in sel.find_all(exp.Sum, exp.Avg):
            if agg.find_ancestor(exp.Select) is not sel:
                continue
            for col in agg.find_all(exp.Column):
                ref = _resolve(col, scope, has_column)
                if ref is None or ref[0] not in fanned or (agg.sql(), ref) in seen:
                    continue
                seen.add((agg.sql(), ref))
                many = fanned[ref[0]]
                note = (hints.columns.get(ref) or {}).get("note")
                findings.append(
                    f"fan-out {agg.key.upper()}: {agg.sql()} uses {ref[0]}.{ref[1]}, but {ref[0]} "
                    f"(one row per {hints.tables[ref[0]].get('grain', ref[0])}) is joined to {many} "
                    f"(one row per {hints.tables.get(many, {}).get('grain', many)}), so each "
                    f"{ref[0]} value repeats once per {many} row and the "
                    + ("total is multiplied" if isinstance(agg, exp.Sum)
                       else f"average is weighted by {many} rows") + " — "
                    f"aggregate {ref[0]} before the join, or take MAX/ANY_VALUE per "
                    f"{hints.tables[ref[0]].get('key', 'key')}"
                    + (f" ({ref[0]}.{ref[1]}: {note})" if note else ""))
    return findings


def check(sql: str, hints: Hints,
          has_column: Optional[Callable[[str, str], bool]] = None) -> list[str]:
    tree = sql_ast.parse_one(sql)
    return check_join_keys(tree, hints, has_column) + check_fanout_sums(tree, hints, has_column)
