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

Filters on a relation's PARTITION column alone are applied when the caller
passes `partition_fraction(table, column, conditions) -> fraction | None`:
on BigQuery that is a dry-run ratio (bytes of the partition column read
with the filter / without), i.e. the share of partitions actually read. It
follows the real distribution of the data — TheLook, like most data, grows
over time, so assuming evenly spread dates would overestimate old periods
and could block wrongly.

Filters `t.col = literal` and `t.col IN (literals)` on a column the registry
profiled are applied as the share of rows they keep. A value with a stored
count (the registry's value counts, columns with few distinct values) keeps
count / rows; any other value keeps 1/ndv — a uniform assumption: a rare
value is overestimated and a common one underestimated (T08: country =
'Japan' is 1/16 by ndv, ~2.5% in the data; 312M estimated, 122.5M real, both
far past the cap). 1/ndv added 2026-10-17 after the test set's T08 (a
filtered fan-out let through); value counts 2026-10-18 after the pool's P11
(a false block: Colombia is 21 of 100,000 users, 1/15 by ndv; 333M
estimated, 1.05M real).

Range filters (`t.col BETWEEN a AND b`, `t.col >= a`, `<`, ...) on a NUMERIC
column whose value counts are complete (every value stored, e.g. users.age:
59 values) keep the exact share of rows in the range; several conditions on
one column are combined into one range first, not multiplied. Added
2026-10-18 for the pool's P09 (age BETWEEN 30 AND 31: a 169.9M-row fan-out
let through). Not min/max interpolation: an even spread over the range can be
far off on skewed data, and acting on such a guess risks false blocks.

`filtered`: some other predicate in the scope is neither a join-key equality
nor one of the filters above (a range on any other column, LIKE, a join
condition on an expression). The estimate ignores those, so it can be far too high: Q28
estimates 8.6M, really 85k (its join has `b.sequence_number =
a.sequence_number + 1`). The Guard therefore acts only on UNFILTERED
estimates (block over the rows cap when the join fans out) and only traces
filtered ones. That choice was made after seeing Q28 on the 30-query tuning
set. On skewed keys the formula runs
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
    filtered: bool  # a predicate it can't apply exists: rows is an upper-side figure
    tables: list[str]
    keys: list[str]  # the equalities used, e.g. "a.user_id = b.user_id"

    @property
    def fanout(self) -> float:
        return self.rows / self.largest_input if self.largest_input else 0.0


def join_estimates(tree: exp.Expression, registry, max_age_days: float,
                   partition_columns: Optional[dict[str, str]] = None,
                   partition_fraction=None) -> list[JoinEstimate]:
    """One estimate per SELECT scope with two or more relations, where every
    input is known. See the module docstring."""
    pcols = {k.lower(): v.lower() for k, v in (partition_columns or {}).items() if v}
    if registry is None:
        return []
    ctes = sql_ast.cte_bodies(tree)
    out = []
    for select in tree.find_all(exp.Select):
        rels = sql_ast._relations(select)
        if len(rels) >= 2:
            est = _scope_estimate(select, rels, ctes, registry, max_age_days, pcols,
                                  partition_fraction)
            if est is not None:
                out.append(est)
    return out


def _is_value(node) -> bool:
    if isinstance(node, exp.Neg):
        node = node.this
    return isinstance(node, (exp.Literal, exp.Boolean))


def _value_text(node) -> str:
    """A literal as the registry stores values (CAST to a string)."""
    if isinstance(node, exp.Neg):
        return "-" + _value_text(node.this)
    if isinstance(node, exp.Boolean):
        return "true" if node.this else "false"
    return str(node.this)


def _value_filter_share(p, aliases, ndv, count, rows):
    """(relation, share of rows kept) for `t.col = literal` or
    `t.col IN (literals)` on a profiled column; None for anything else.
    Per value: its stored count / rows, else 1/ndv."""
    if isinstance(p, exp.EQ):
        col, val = p.this, p.expression
        if isinstance(val, exp.Column):
            col, val = val, col
        if not (isinstance(col, exp.Column) and _is_value(val)):
            return None
        vals = [val]
    elif isinstance(p, exp.In) and not p.args.get("query") and not p.args.get("unnest"):
        col, vals = p.this, p.expressions
        if not (isinstance(col, exp.Column) and vals and all(_is_value(v) for v in vals)):
            return None
        vals = list({v.sql(): v for v in vals}.values())
    else:
        return None
    alias = col.table.lower()
    if alias not in aliases:
        return None
    i = aliases.index(alias)
    name = col.name.lower()
    share = 0.0
    for v in vals:
        n = count(i, name, _value_text(v))
        if n is not None and rows[i]:
            share += n / rows[i]
            continue
        d = ndv(i, name)
        if not d:
            return None
        share += 1 / d
    return i, min(1.0, share)


_RANGE_OPS = {exp.GT: (">", False), exp.GTE: (">", True), exp.LT: ("<", False), exp.LTE: ("<", True)}
_FLIP = {">": "<", "<": ">"}


def _number(node) -> Optional[float]:
    if isinstance(node, exp.Neg):
        v = _number(node.this)
        return -v if v is not None else None
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return float(node.this)
        except ValueError:
            return None
    return None


def _range_bounds(p, aliases):
    """(relation, column, [(side, value, inclusive)]) for `t.col BETWEEN a AND
    b` or `t.col <op> number` (either order); None for anything else."""
    if isinstance(p, exp.Between):
        col, lo, hi = p.this, _number(p.args.get("low")), _number(p.args.get("high"))
        if not isinstance(col, exp.Column) or lo is None or hi is None:
            return None
        bounds = [(">", lo, True), ("<", hi, True)]
    elif type(p) in _RANGE_OPS:
        side, inclusive = _RANGE_OPS[type(p)]
        col, val = p.this, _number(p.expression)
        if not isinstance(col, exp.Column):
            col, val, side = p.expression, _number(p.this), _FLIP[side]
        if not isinstance(col, exp.Column) or val is None:
            return None
        bounds = [(side, val, inclusive)]
    else:
        return None
    alias = (col.table or "").lower()
    if alias not in aliases:
        return None
    return aliases.index(alias), col.name.lower(), bounds


def _in_range(x: float, bounds) -> bool:
    for side, v, inclusive in bounds:
        if side == ">" and not (x > v or (inclusive and x == v)):
            return False
        if side == "<" and not (x < v or (inclusive and x == v)):
            return False
    return True


def _fresh(entry, max_age_days):
    return entry is not None and entry.age_days() <= max_age_days


def _scope_estimate(select, rels, ctes, registry, max_age_days, pcols=None,
                    partition_fraction=None) -> Optional[JoinEstimate]:
    pcols = pcols or {}
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

    def range_share(i: int, col: str, bounds) -> Optional[float]:
        """Exact share of rows of relation i inside `bounds`, from complete
        value counts of a numeric column; None when not known exactly."""
        e = registry.lookup(tables[i], col)
        if not (_fresh(e, max_age_days) and hasattr(registry, "complete_value_counts")
                and e.total_rows and not str(e.data_type).upper().startswith(
                    ("STRING", "VARCHAR", "DATE", "TIMESTAMP", "DATETIME", "TIME", "BOOL"))):
            return None
        counts = registry.complete_value_counts(tables[i], col)
        if counts is None:
            return None
        try:
            kept = sum(n for v, n in counts.items() if _in_range(float(v), bounds))
        except ValueError:
            return None
        return kept / e.total_rows

    def count(i: int, col: str, value: str) -> Optional[int]:
        if not (hasattr(registry, "value_count")
                and _fresh(registry.lookup(tables[i], col), max_age_days)):
            return None
        return registry.value_count(tables[i], col, value)

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
    on_partition: dict[int, list[exp.Expression]] = {}  # rel -> its partition-column filters
    value_share: dict[int, float] = {}  # rel -> share of rows its value filters keep
    ranges: dict[tuple[int, str], list] = {}  # (rel, column) -> range bounds, combined
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
        owners = {c.table.lower() for c in cols}
        if (partition_fraction is not None and len(owners) == 1 and owners <= set(aliases)
                and not any(isinstance(n, exp.Subquery) for n in p.walk())):
            i = aliases.index(next(iter(owners)))
            if all(c.name.lower() == pcols.get(tables[i]) for c in cols):
                on_partition.setdefault(i, []).append(p)
                continue
        sel = _value_filter_share(p, aliases, ndv, count, table_rows)
        if sel is not None:
            i, share = sel
            value_share[i] = value_share.get(i, 1.0) * share
            continue
        rng = _range_bounds(p, aliases)
        if rng is not None:
            i, col, bounds = rng
            ranges.setdefault((i, col), []).extend(bounds)
            continue
        filtered = True

    for (i, col), bounds in ranges.items():
        share = range_share(i, col, bounds)
        if share is None:
            filtered = True  # a range not known exactly: the estimate stays traced only
        else:
            value_share[i] = value_share.get(i, 1.0) * share

    for i, share in value_share.items():
        table_rows[i] = max(1, round(table_rows[i] * share))

    for i, conds in on_partition.items():
        bare = []
        for p in conds:
            q = p.copy()
            for c in q.find_all(exp.Column):
                c.set("table", None)
            bare.append(q)
        frac = partition_fraction(tables[i], pcols[tables[i]], bare)
        if frac is None:
            filtered = True
        else:
            table_rows[i] = max(1, round(table_rows[i] * min(1.0, frac)))

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
