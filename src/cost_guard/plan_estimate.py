"""EXPLAIN plan cardinalities, corrected with the column registry.

DuckDB's plan already gives per-operator cardinality estimates, including
join fan-out. Where they go wrong is when the planner guesses a column's
distinct count — e.g. a window aggregate planned as GROUP BY + join back,
where the GROUP BY on a 2-value column is estimated at 100,000 groups and the
join inherits a ~100x over-estimate. The column registry knows those distinct
counts, so this module walks the plan and corrects estimates where the
registry gives a real bound.

Rules:
1. GROUP BY (a bound — only ever lowers): output <= product over group keys
   of (registry distinct count x 1.05 for HyperLogLog error, +1 if the column
   has NULLs — NULL is its own group). Applied only when every key traces
   back to a base column with a fresh registry entry.
2. Scans (via the backend's scan hook, which has the table sizes):
   - a scan with no filter reads every row. DuckDB removes a range filter it
     proves always-true from its own statistics but keeps a flat-selectivity
     estimate (20% of the table) — that is raised to the table's row count;
   - a range filter that remains is estimated from the registry's min/max:
     the fraction of the column's value range it covers, assuming values are
     spread evenly. That assumption runs low on skewed data, so it is used
     RAISE-ONLY: it lifts DuckDB's flat guess when the range is wider, and
     never lowers it (an underestimate could let a costly query under the cap).
3. Join: recomputed only when an input below it changed, as
   rows(L) x rows(R) / max(distinct(L.key), distinct(R.key)) on the
   corrected inputs (distinct capped at the input's rows); keys that can't
   be traced fall back to scaling DuckDB's estimate by the inputs' change
   ratios. It can only go above DuckDB's figure if an input went up. A join
   whose inputs are unchanged keeps DuckDB's estimate — so a real self-join
   blow-up is never "corrected" away.
4. Every other operator scales with its corrected input.

Columns are traced through the plan: scans name their table and columns,
projections pass `#i` positions through (including DuckDB's internal
compression wrappers), GROUP BY outputs its keys first, joins output left
then right. Anything that can't be traced is left uncorrected.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

HLL_MARGIN = 1.05  # registry distinct counts are approximate (~2% error)

_POS = re.compile(r"^#(\d+)$")
_WRAP = re.compile(r"^__internal_\w+\(\s*(#\d+)\s*(?:,[^()]*)?\)$")
_IDENT = re.compile(r"^[A-Za-z_][\w]*$")
_EQ = re.compile(r"^(.+?)\s+(?:=|IS NOT DISTINCT FROM)\s+(.+)$")
_PRODUCT_OPS = {"CROSS_PRODUCT", "NESTED_LOOP_JOIN", "BLOCKWISE_NL_JOIN"}
_GROUP_OPS = {"HASH_GROUP_BY", "PERFECT_HASH_GROUP_BY"}
_PASS_OPS = {"FILTER", "ORDER_BY", "TOP_N", "LIMIT", "STREAMING_LIMIT", "WINDOW",
             "STREAMING_WINDOW"}
_LEFT_ONLY_JOINS = {"SEMI", "ANTI", "MARK", "RIGHT_SEMI", "RIGHT_ANTI"}

Lineage = Optional[tuple]  # (schema, table, column) or None


@dataclass
class _Node:
    name: str
    orig: int  # DuckDB's estimate
    est: int  # corrected estimate
    cols: list = field(default_factory=list)  # output position -> Lineage
    scanned: int = 0


@dataclass
class PlanCardinality:
    peak: int  # largest corrected intermediate row count
    peak_uncorrected: int  # DuckDB's own peak
    scanned: int  # rows from scan operators
    corrections: list[str] = field(default_factory=list)


def _as_list(v) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def _resolve(expr: str, child_cols: list) -> Lineage:
    expr = expr.strip()
    m = _POS.match(expr)
    if m:
        i = int(m.group(1))
        return child_cols[i] if i < len(child_cols) else None
    m = _WRAP.match(expr)
    if m:
        return _resolve(m.group(1), child_cols)
    if _IDENT.match(expr):
        hits = [c for c in child_cols if c is not None and c[2].lower() == expr.lower()]
        return hits[0] if hits else None
    return None


# (schema, table, filters or None, planner estimate) -> (new estimate, note) | None
ScanHook = Callable[[str, str, Optional[str], int], Optional[tuple]]


class _Corrector:
    def __init__(self, registry, max_age_days: float, scan_hook: Optional[ScanHook] = None):
        self.registry = registry
        self.max_age_days = max_age_days
        self.scan_hook = scan_hook
        self.corrections: list[str] = []
        self.nodes: list[_Node] = []  # every node walked, for the peak

    def ndv_bound(self, lin: Lineage) -> Optional[int]:
        if lin is None or self.registry is None:
            return None
        schema, table, column = lin
        e = self.registry.lookup(table, column, schema)
        if e is None or e.age_days() > self.max_age_days:
            return None
        return math.ceil(e.distinct_count * HLL_MARGIN) + (1 if e.null_count else 0)

    @staticmethod
    def scaled(orig: int, kid: "_Node") -> int:
        if kid.orig and kid.est != kid.orig:
            return max(1, round(orig * kid.est / kid.orig))
        return orig

    def walk(self, n: dict) -> _Node:
        kids = [self.walk(c) for c in n.get("children", [])]
        info = n.get("extra_info") or {}
        name = (n.get("name") or "").upper()
        raw = info.get("Estimated Cardinality")
        if raw is not None:
            orig = int(raw)
        elif name == "UNGROUPED_AGGREGATE":
            orig = 1
        elif name in _PRODUCT_OPS and kids:
            orig = math.prod(max(k.orig, 1) for k in kids)
        else:
            orig = max((k.orig for k in kids), default=0)
        node = _Node(name, orig, orig, scanned=sum(k.scanned for k in kids))

        if "SCAN" in name and "Table" in info:
            parts = str(info["Table"]).split(".")
            schema = parts[-2] if len(parts) >= 2 else "main"
            node.cols = [(schema, parts[-1], c) for c in _as_list(info.get("Projections"))]
            if self.scan_hook is not None:
                filters = info.get("Filters")
                if isinstance(filters, list):
                    filters = " AND ".join(filters)
                fixed = self.scan_hook(schema, parts[-1], filters or None, orig)
                if fixed is not None:
                    node.est, note = fixed
                    self.corrections.append(note)
            node.scanned += node.est
        elif name == "PROJECTION" and kids:
            node.cols = [_resolve(e, kids[0].cols) for e in _as_list(info.get("Projections"))]
            node.est = self.scaled(orig, kids[0])
        elif name in _PASS_OPS and kids:
            node.cols = list(kids[0].cols)
            node.est = self.scaled(orig, kids[0])
        elif name in _GROUP_OPS and kids:
            keys = [_resolve(g, kids[0].cols) for g in _as_list(info.get("Groups"))]
            node.cols = keys + [None] * len(_as_list(info.get("Aggregates")))
            node.est = min(self.scaled(orig, kids[0]), kids[0].est)
            bounds = [self.ndv_bound(k) for k in keys]
            if keys and all(b is not None for b in bounds):
                bound = math.prod(bounds)
                if bound < node.est:
                    cols = ", ".join(f"{k[1]}.{k[2]}" for k in keys)
                    self.corrections.append(
                        f"GROUP BY {cols}: at most {bound:,} groups per the column "
                        f"registry (planner estimated {orig:,})"
                    )
                    node.est = bound
        elif name == "UNGROUPED_AGGREGATE":
            node.est = 1
        elif len(kids) == 2 and ("JOIN" in name or name in _PRODUCT_OPS):
            node.est = self._join(name, info, orig, kids, node)
        elif len(kids) == 1:
            node.est = self.scaled(orig, kids[0])
        self.nodes.append(node)
        return node

    def _join(self, name: str, info: dict, orig: int, kids: list, node: _Node) -> int:
        left, right = kids
        jtype = str(info.get("Join Type") or "INNER").upper()
        node.cols = list(left.cols) if jtype in _LEFT_ONLY_JOINS else left.cols + right.cols
        if name in _PRODUCT_OPS and not info.get("Conditions"):
            return max(left.est, 1) * max(right.est, 1)
        if left.est == left.orig and right.est == right.orig:
            return orig  # DuckDB's estimate stands: nothing below it changed
        raised = left.est > left.orig or right.est > right.orig
        pairs = []
        for cond in _as_list(info.get("Conditions")):
            for part in re.split(r"\s+AND\s+", cond):
                m = _EQ.match(part.strip())
                if m:
                    pairs.append((_resolve(m.group(1), left.cols), _resolve(m.group(2), right.cols)))
        ndvs = []
        for lk, rk in pairs:
            lb, rb = self.ndv_bound(lk), self.ndv_bound(rk)
            if lb is None or rb is None:
                ndvs = []
                break
            ndvs.append(max(min(lb, max(left.est, 1)), min(rb, max(right.est, 1))))
        if ndvs:
            # Largest per-key distinct count: the conservative (larger) estimate.
            new = max(1, round(left.est * right.est / max(ndvs)))
            how = "join keys' distinct counts from the column registry"
        else:
            ratio = (left.est / left.orig if left.orig else 1) * (right.est / right.orig if right.orig else 1)
            new = max(1, round(orig * ratio))
            how = "planner selectivity on corrected inputs"
        if jtype in ("LEFT", "OUTER", "FULL"):
            new = max(new, left.est)
        if not raised:
            new = min(orig, new)  # only-lowered inputs can't raise the join
        if new != orig:
            self.corrections.append(
                f"{name} ({jtype}): ~{new:,} rows from corrected inputs "
                f"({left.est:,} x {right.est:,}; {how}), planner estimated {orig:,}"
            )
        return new


def plan_cardinality(plans: list[dict], registry=None, max_age_days: float = 35,
                     scan_hook: Optional[ScanHook] = None) -> PlanCardinality:
    """Peak and scanned rows for an EXPLAIN (FORMAT JSON) plan list, with
    registry corrections when `registry` is given and scan corrections when
    `scan_hook` is given."""
    c = _Corrector(registry, max_age_days, scan_hook)
    roots = [c.walk(p) for p in plans]
    return PlanCardinality(
        peak=max((n.est for n in c.nodes), default=0),
        peak_uncorrected=max((n.orig for n in c.nodes), default=0),
        scanned=sum(n.scanned for n in roots),
        corrections=list(dict.fromkeys(c.corrections)),  # a table scanned twice: one note
    )
