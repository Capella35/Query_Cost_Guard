"""Join output estimate from the SQL and the column registry.

For warehouses whose estimate has no row counts: a BigQuery dry run reports
bytes READ, and a join that multiplies rows reads few bytes (Q13: a self-join
on events.user_id reads ~10 MiB and produces ~100M rows). DuckDB does not
need this — its plan estimate already has join cardinalities.

Per SELECT scope whose relations are tables (or views) linked by column
equalities (ON, USING, or WHERE a.x = b.y), left to right:

    rows(L join R) = rows(L) x rows(R) / max(ndv(L.key), ndv(R.key))

the textbook estimate that DuckDB's planner and plan_estimate.py also use.
Row counts and distinct counts (ndv) come from the column registry (fresh
entries only, so views are covered too). Per side, a composite key's ndv is
the product of its columns' ndv, capped at that side's rows. An outer join
returns at least its preserved side.

No estimate (None) when anything is unknown: a relation that is not a table
(subquery, CTE, UNNEST), a missing or stale registry entry, a relation with
no equality to the ones before it, an unqualified column in a predicate.

`filtered`: some predicate in the scope is not a join-key equality (a
WHERE filter, or a join condition on an expression). The estimate uses
whole-table rows and ignores those predicates, so it can be far too high:
Q28 estimates 8.6M, really 85k (1.75M even without its date filters). The
Guard therefore acts only on UNFILTERED estimates (block over the rows cap
when the join fans out) and only traces filtered ones. That choice was made
after seeing Q28 on the 30-query tuning set. On skewed keys the formula runs
LOW (Q13: ~74M estimated, ~100M real), so a block is not an artefact of
overestimating.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlglot import exp

from . import sql_ast

# A join "fans out" when its estimated output is at least this many times its
# largest input. A many-to-one lookup join returns about the larger table's
# rows; HyperLogLog error alone can push its estimate a few % above that.
# 2x is a judgment call, not a measured threshold.
FANOUT_MIN_RATIO = 2.0


@dataclass
class JoinEstimate:
    rows: int  # estimated output rows of the scope's joins
    largest_input: int  # rows of the largest table joined
    filtered: bool  # a non-key predicate exists: rows is an upper-side figure
    tables: list[str]
    keys: list[str]  # the equalities used, e.g. "a.user_id = b.user_id"

    @property
    def fanout(self) -> float:
        return self.rows / self.largest_input if self.largest_input else 0.0


def join_estimates(tree: exp.Expression, registry, max_age_days: float) -> list[JoinEstimate]:
    """One estimate per SELECT scope with two or more relations, where every
    input is known. See the module docstring."""
    if registry is None:
        return []
    ctes = sql_ast.cte_bodies(tree)
    out = []
    for select in tree.find_all(exp.Select):
        rels = sql_ast._relations(select)
        if len(rels) >= 2:
            est = _scope_estimate(select, rels, ctes, registry, max_age_days)
            if est is not None:
                out.append(est)
    return out


def _fresh(entry, max_age_days):
    return entry is not None and entry.age_days() <= max_age_days


def _scope_estimate(select, rels, ctes, registry, max_age_days) -> Optional[JoinEstimate]:
    aliases: list[str] = []
    tables: list[str] = []
    table_rows: list[int] = []
    for rel, _ in rels:
        if not isinstance(rel, exp.Table) or rel.name.lower() in ctes:
            return None
        t = rel.name.lower()
        entry = next((e for e in registry.entries() if e.table_name.lower() == t), None)
        if not _fresh(entry, max_age_days):
            return None
        aliases.append(sql_ast._rel_name(rel))
        tables.append(t)
        table_rows.append(int(entry.total_rows))

    def ndv(i: int, col: str) -> Optional[int]:
        e = registry.lookup(tables[i], col)
        return int(e.distinct_count) if _fresh(e, max_age_days) else None

    # Join-key equalities between two relations (ON, USING, WHERE); anything
    # else marks the scope filtered.
    preds = sql_ast.conjuncts(select.args.get("where"))
    using: dict[int, list[str]] = {}
    for idx, (_, join) in enumerate(rels):
        if join is None:
            continue
        preds.extend(sql_ast.conjuncts(join.args.get("on")))
        if join.args.get("using"):
            using[idx] = [c.name.lower() for c in join.args["using"]]
    edges: list[tuple[int, str, int, str]] = []  # (rel i, col, rel j, col)
    filtered = False
    for p in preds:
        cols = list(p.find_all(exp.Column))
        if any(not c.table for c in cols):
            return None  # can't tell which relation an unqualified column belongs to
        if (isinstance(p, exp.EQ) and isinstance(p.this, exp.Column)
                and isinstance(p.expression, exp.Column)):
            a, b = p.this, p.expression
            ia = aliases.index(a.table.lower()) if a.table.lower() in aliases else None
            ib = aliases.index(b.table.lower()) if b.table.lower() in aliases else None
            if ia is not None and ib is not None and ia != ib:
                edges.append((ia, a.name.lower(), ib, b.name.lower()))
                continue
        filtered = True

    rows = table_rows[0]
    keys_used: list[str] = []
    for k in range(1, len(rels)):
        pairs = []  # (left rel, left col, right col) with the left rel before k
        for i, ci, j, cj in edges:
            if j == k and i < k:
                pairs.append((i, ci, cj))
            elif i == k and j < k:
                pairs.append((j, cj, ci))
        for col in using.get(k, []):
            left = next((i for i in range(k) if ndv(i, col) is not None), None)
            if left is None:
                return None
            pairs.append((left, col, col))
        if not pairs:
            return None  # no link: the cartesian rule's case, not this estimate's
        div_l = div_r = 1
        for i, ci, cj in pairs:
            nl, nr = ndv(i, ci), ndv(k, cj)
            if nl is None or nr is None:
                return None
            div_l *= max(nl, 1)
            div_r *= max(nr, 1)
            keys_used.append(f"{aliases[i]}.{ci} = {aliases[k]}.{cj}")
        div_l, div_r = min(div_l, rows), min(div_r, table_rows[k])
        joined = rows * table_rows[k] // max(div_l, div_r, 1)
        side = (rels[k][1].args.get("side") or "").upper()
        if side == "LEFT":
            joined = max(joined, rows)
        elif side == "RIGHT":
            joined = max(joined, table_rows[k])
        elif side == "FULL":
            joined = max(joined, rows, table_rows[k])
        rows = joined
    return JoinEstimate(rows=rows, largest_input=max(table_rows), filtered=filtered,
                        tables=tables, keys=keys_used)
