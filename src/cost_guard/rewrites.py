"""Deterministic rewrite fixers — no LLM needed.

Each fixer turns a detected anti-pattern into an EQUIVALENT query (same
rows, same columns) that is cheaper or at least no costlier. Every fixer
output MUST pass compare_results before the Guard offers it; an unverified
rewrite is never shown to the analyst.

Catalog:
- repeated subquery -> CTE (suggest_cte_rewrite): edits the analyst's own
  text, so they see their query, not a reformatted one;
- function on a date column -> range (suggest_sargable_rewrite):
  DATE_TRUNC(unit, col) = X  and  CAST(col AS DATE) = X  become a range on
  the bare column, which zone maps / partitions can prune;
- filter after join -> pushed into the derived table
  (suggest_pushdown_rewrite).
The last two rebuild the query from the AST (sqlglot formatting).

Not in the catalog, on purpose: SELECT * -> column list and "add a date
filter". Both change the result (fewer columns / fewer rows), so no
equivalent rewrite exists; they stay warnings for a human to decide.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlglot import exp

from . import sql_ast


def parenthesized_subqueries(sql: str) -> list[str]:
    """Extract (SELECT ...) blocks as raw text spans, respecting nested
    parens and ignoring parens and keywords inside string literals."""
    n = len(sql)
    # Mask string literals so their contents never count as parens/keywords.
    masked = list(sql.lower())
    i = 0
    while i < n:
        if masked[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                masked[k] = " "
            i = j + 1
        else:
            i += 1
    low = "".join(masked)

    out, i = [], 0
    while True:
        j = low.find("select", i)
        if j == -1:
            break
        k = j - 1
        while k >= 0 and low[k] in " \t\n\r":
            k -= 1
        if k >= 0 and low[k] == "(":
            depth, m = 0, k
            while m < n:
                if low[m] == "(":
                    depth += 1
                elif low[m] == ")":
                    depth -= 1
                    if depth == 0:
                        out.append(sql[k : m + 1])
                        break
                m += 1
            i = m + 1
        else:
            i = j + 6
    return out


def _key(span: str) -> Optional[str]:
    """Normalized SQL of a parenthesized subquery span (AST-based)."""
    tree = sql_ast.try_parse(span[1:-1])
    return tree.sql(dialect=sql_ast.DIALECT).lower() if tree is not None else None


def suggest_cte_rewrite(sql: str) -> Optional[str]:
    """Hoist a repeated subquery into a CTE.

    Returns the rewritten SQL, or None when no repeated subquery is found or
    the result would not parse. Handles the first duplicated subquery only.
    """
    tree = sql_ast.try_parse(sql)
    if tree is None:
        return None
    reps = sql_ast.repeated_subqueries(tree)
    if not reps:
        return None
    target = reps[0][0]
    spans = [s for s in parenthesized_subqueries(sql) if _key(s) == target]
    if len(spans) < 2:
        return None
    name, i = "cte_1", 2
    while re.search(rf"\b{name}\b", sql, re.IGNORECASE):
        name, i = f"cte_{i}", i + 1
    inner = spans[0][1:-1].strip()
    body = sql
    for raw in dict.fromkeys(spans):
        body = body.replace(raw, name)
    if tree.args.get("with") is not None:
        # Prepend to the existing WITH list.
        new_sql = re.sub(r"(?is)^\s*with\s+", f"WITH {name} AS ({inner}), ", body, count=1)
    else:
        new_sql = f"WITH {name} AS ({inner}) " + body
    return new_sql if sql_ast.try_parse(new_sql) is not None else None


_TRUNC_UNITS = {"YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "HOUR", "MINUTE"}


def _const(e: exp.Expression) -> bool:
    return e.find(exp.Column) is None and not any(
        isinstance(n, exp.Func) and not isinstance(n, exp.Cast) for n in e.walk()
    )


def _typed(x: exp.Expression) -> exp.Expression:
    """An untyped string literal gets an explicit TIMESTAMP type so date
    arithmetic on it is well-defined; typed constants are left alone."""
    if isinstance(x, exp.Literal) and x.is_string:
        return exp.cast(x.copy(), "TIMESTAMP")
    return x.copy()


def _sargable(cmp: exp.Expression) -> Optional[exp.Expression]:
    """Range replacement for one `f(col) = X` predicate, or None."""
    if not isinstance(cmp, exp.EQ):
        return None
    for fn, x in ((cmp.this, cmp.expression), (cmp.expression, cmp.this)):
        if not _const(x) or (isinstance(x, exp.Literal) and not x.is_string):
            continue
        if isinstance(fn, (exp.TimestampTrunc, exp.DateTrunc)) and isinstance(fn.this, exp.Column):
            unit = (fn.args.get("unit").name if fn.args.get("unit") is not None else "").upper()
            if unit not in _TRUNC_UNITS:
                return None
            col, lo = fn.this, _typed(x)
            hi = exp.Add(this=_typed(x), expression=exp.Interval(
                this=exp.Literal.string("1"), unit=exp.Var(this=unit)))
            aligned = exp.EQ(this=exp.TimestampTrunc(this=_typed(x), unit=exp.Var(this=unit)),
                             expression=_typed(x))
            # DATE_TRUNC(u, col) = X  <=>  X is u-aligned AND X <= col < X + 1 u
            return exp.and_(exp.GTE(this=col.copy(), expression=lo),
                            exp.LT(this=col.copy(), expression=hi), aligned)
        if (isinstance(fn, exp.Cast) and isinstance(fn.this, exp.Column)
                and fn.to.this == exp.DataType.Type.DATE):
            col, day = fn.this, exp.cast(x.copy(), "DATE")
            hi = exp.Add(this=day.copy(), expression=exp.Interval(
                this=exp.Literal.string("1"), unit=exp.Var(this="DAY")))
            # CAST(col AS DATE) = X  <=>  X <= col < X + 1 day
            return exp.and_(exp.GTE(this=col.copy(), expression=day), exp.LT(this=col.copy(), expression=hi))
    return None


def suggest_sargable_rewrite(sql: str) -> Optional[str]:
    """Replace `DATE_TRUNC(unit, col) = X` / `CAST(col AS DATE) = X` WHERE
    conjuncts with an equivalent range on the bare column. None when there
    is nothing to rewrite."""
    tree = sql_ast.try_parse(sql)
    if tree is None:
        return None
    tree = tree.copy()
    changed = False
    for where in list(tree.find_all(exp.Where)):
        for conj in sql_ast.conjuncts(where):
            new = _sargable(conj)
            if new is not None:
                conj.replace(exp.Paren(this=new))
                changed = True
    if not changed:
        return None
    out = tree.sql(dialect=sql_ast.DIALECT)
    return out if sql_ast.try_parse(out) is not None else None


def _pushable_inner(inner: exp.Expression) -> Optional[dict]:
    """alias -> expression map for a derived table whose WHERE can take an
    outer filter without changing results; None otherwise."""
    if not isinstance(inner, exp.Select):
        return None
    for arg in ("group", "having", "distinct", "limit", "offset", "qualify", "with"):
        if inner.args.get(arg) is not None:
            return None
    if inner.find(exp.Window) is not None:
        return None
    mapping = {}
    for e in inner.expressions:
        if isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)):
            return None
        target = e.unalias()
        if target.find(exp.AggFunc) is not None or target.find(exp.Rand) is not None:
            return None
        mapping[e.alias_or_name.lower()] = target
    return mapping


def suggest_pushdown_rewrite(sql: str) -> Optional[str]:
    """Move outer WHERE conjuncts on a joined derived table into that derived
    table's WHERE (before its rows leave the join). Equivalent when the
    derived table has no aggregation, DISTINCT, LIMIT or window — its own
    WHERE is applied after its join anyway — and the outer query reads it
    alone (no other relations). None when nothing can be pushed."""
    tree = sql_ast.try_parse(sql)
    if tree is None:
        return None
    tree = tree.copy()
    changed = False
    for outer in list(tree.find_all(exp.Select)):
        from_ = outer.args.get("from_")
        where = outer.args.get("where")
        if from_ is None or where is None or outer.args.get("joins"):
            continue
        sub = from_.this
        if not isinstance(sub, exp.Subquery) or not sub.this.args.get("joins"):
            continue
        mapping = _pushable_inner(sub.this)
        if mapping is None:
            continue
        alias = (sub.alias or "").lower()
        keep, push = [], []
        for conj in sql_ast.conjuncts(where):
            cols = list(conj.find_all(exp.Column))
            ok = (cols and conj.find(exp.Subquery) is None and conj.find(exp.AggFunc) is None
                  and all((not c.table or c.table.lower() == alias)
                          and c.name.lower() in mapping for c in cols))
            (push if ok else keep).append(conj)
        if not push:
            continue
        inner = sub.this
        for conj in push:
            moved = conj.copy()
            for c in list(moved.find_all(exp.Column)):
                target = mapping[c.name.lower()].copy()
                c.replace(target if isinstance(target, exp.Column) else exp.Paren(this=target))
            inner.where(moved, copy=False)
        if keep:
            outer.set("where", exp.Where(this=exp.and_(*[k.copy() for k in keep])))
        else:
            outer.set("where", None)
        changed = True
    if not changed:
        return None
    out = tree.sql(dialect=sql_ast.DIALECT)
    return out if sql_ast.try_parse(out) is not None else None


# Fixers that only restate a filter in a form the engine can prune on: same
# predicate, same table, same rows. They read no more than the original, so the
# Guard does not hold them to the "not costlier by estimate" check — the plan
# estimates the two forms differently (DuckDB: an equality on CAST(col AS DATE)
# from the distinct count, a range at a flat 20%), which says nothing about
# cost. compare_results still verifies them.
PRUNING_ONLY_FIXERS = frozenset({"sargable"})


def deterministic_rewrite(sql: str, findings: list[str]) -> tuple[Optional[str], list[str]]:
    """Apply every catalog fixer the findings call for, chained. Returns
    (rewritten SQL or None, fixer names applied)."""
    current, applied = sql, []
    steps = [
        ("cte", lambda f: "CTE" in f, suggest_cte_rewrite),
        ("sargable", lambda f: "wrapped in an expression" in f, suggest_sargable_rewrite),
        ("pushdown", lambda f: "applied after the join" in f, suggest_pushdown_rewrite),
    ]
    for name, wanted, fixer in steps:
        if any(wanted(f) for f in findings):
            out = fixer(current)
            if out and out.strip() != current.strip():
                current, applied = out, applied + [name]
    return (current if applied else None), applied
