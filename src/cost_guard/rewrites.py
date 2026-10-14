"""Deterministic rewrite fixers — no LLM needed.

Each fixer turns a detected anti-pattern into a cheaper, equivalent query.
Every fixer output MUST pass compare_results before the Guard offers it;
an unverified rewrite is never shown to the analyst.

Detection is on the sqlglot AST; the rewrite edits the analyst's own text
(so they see their query, not a reformatted one) and must re-parse.
"""
from __future__ import annotations

import re
from typing import Optional

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
