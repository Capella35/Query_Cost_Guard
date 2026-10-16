"""SQL structure from a real parser (sqlglot AST) — deterministic, no regex.

Every structural question the Guard asks about a query lives here: is it
read-only, which physical tables does it read, which relations are joined
without a linking predicate, is the partition column filtered (bare or
wrapped in a function), are filters applied after a join, which subqueries
repeat. Parsing is a parsing problem, not an LLM problem.

The dialect is DuckDB unless a caller sets another for a block of work with
using_dialect("bigquery") — the Guard does that per decision from the
backend, so BigQuery SQL (backticked names, BigQuery functions) is read as
BigQuery SQL. An explicit dialect= argument always wins.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

DIALECT = "duckdb"
_CURRENT_DIALECT: contextvars.ContextVar[str] = contextvars.ContextVar("sql_dialect", default=DIALECT)


@contextmanager
def using_dialect(dialect: str):
    """Parse as `dialect` inside this block (per thread/task)."""
    token = _CURRENT_DIALECT.set(dialect)
    try:
        yield
    finally:
        _CURRENT_DIALECT.reset(token)


def current_dialect() -> str:
    return _CURRENT_DIALECT.get()

# Relations at or below this many rows are "provably tiny" for the cartesian
# rule: crossing a 10-row dimension table is not a runaway.
TINY_RELATION_ROWS = 1_000

# Functions that read files, the network, or the environment. A read-only
# SELECT that calls one is still a data-exfiltration path, so it blocks.
DENIED_FUNCTIONS = frozenset(
    {
        "read_csv", "read_csv_auto", "read_parquet", "parquet_scan",
        "parquet_metadata", "parquet_schema", "parquet_file_metadata",
        "parquet_kv_metadata", "read_json", "read_json_auto",
        "read_json_objects", "read_json_objects_auto", "read_ndjson",
        "read_ndjson_auto", "read_ndjson_objects", "read_text", "read_blob",
        "read_xlsx", "sniff_csv", "glob", "iceberg_scan", "iceberg_metadata",
        "delta_scan", "sqlite_scan", "sqlite_attach", "postgres_scan",
        "postgres_query", "mysql_scan", "mysql_query", "getenv",
        "query", "query_table", "http_get", "http_post",
    }
)
# Table-valued functions that only generate values (no I/O).
ALLOWED_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})


class SQLParseError(ValueError):
    """The query does not parse. Callers fail closed on this."""


@lru_cache(maxsize=512)
def _parse_cached(sql: str, dialect: str) -> tuple:
    try:
        stmts = sqlglot.parse(sql, read=dialect)
    except (ParseError, TokenError) as e:
        raise SQLParseError(str(e).splitlines()[0]) from e
    return tuple(s for s in stmts if s is not None)


def parse_statements(sql: str, dialect: Optional[str] = None) -> tuple:
    """All statements in the input. The trees are shared (cached): read them,
    and .copy() before mutating."""
    if not sql or not sql.strip():
        raise SQLParseError("empty query")
    return _parse_cached(sql, dialect or current_dialect())


def parse_one(sql: str, dialect: Optional[str] = None) -> exp.Expression:
    stmts = parse_statements(sql, dialect)
    if len(stmts) != 1:
        raise SQLParseError(f"expected exactly one statement, got {len(stmts)}")
    return stmts[0]


def try_parse(sql: str, dialect: Optional[str] = None) -> Optional[exp.Expression]:
    try:
        return parse_one(sql, dialect)
    except SQLParseError:
        return None


# --------------------------------------------------------------------------- #
# Read-only classification (F1.5)
# --------------------------------------------------------------------------- #


def _func_name(f: exp.Func) -> str:
    if isinstance(f, exp.Anonymous):
        return str(f.name).lower()
    return f.sql_name().lower()


def read_only_violation(sql: str, dialect: Optional[str] = None) -> Optional[str]:
    """None when the query is a single read-only SELECT/WITH; otherwise the
    reason it is not. Fails closed: unparseable input is a violation."""
    try:
        stmts = parse_statements(sql, dialect)
    except SQLParseError as e:
        return f"does not parse ({e}) — failing closed"
    if len(stmts) != 1:
        return f"{len(stmts)} statements — exactly one SELECT/WITH allowed"
    root = stmts[0]
    if not isinstance(root, (exp.Select, exp.SetOperation, exp.Subquery)):
        return f"{type(root).__name__.upper()} statement is not a read-only SELECT/WITH"
    # Data-modifying CTEs or any write node anywhere in the tree.
    write_nodes = (
        exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
        exp.Alter, exp.Command, exp.Copy, exp.Set, exp.Pragma, exp.Grant,
        exp.Revoke, exp.Attach, exp.Detach, exp.Install, exp.TruncateTable,
    )
    for node in root.walk():
        if isinstance(node, write_nodes):
            return f"contains a {type(node).__name__.upper()} node"
    for f in root.find_all(exp.Func):
        name = _func_name(f)
        if name in DENIED_FUNCTIONS:
            return f"calls {name}() — file/network/environment access is denied"
    for t in root.find_all(exp.Table):
        if isinstance(t.this, exp.Func):
            name = _func_name(t.this)
            if name not in ALLOWED_TABLE_FUNCTIONS:
                return f"table function {name}() is not on the allow list"
        elif isinstance(t.this, exp.Identifier) and t.this.quoted and any(
            ch in t.name for ch in "./\\:"
        ):
            return f"FROM {t.name!r} reads a file by path"
    return None


def is_read_only(sql: str, dialect: Optional[str] = None) -> bool:
    return read_only_violation(sql, dialect) is None


# --------------------------------------------------------------------------- #
# Tables, CTEs, GROUP BY, subqueries
# --------------------------------------------------------------------------- #


def cte_bodies(tree: exp.Expression) -> dict[str, exp.Expression]:
    return {cte.alias.lower(): cte.this for cte in tree.find_all(exp.CTE)}


def referenced_tables(tree: exp.Expression) -> list[str]:
    """Physical tables the query reads (CTE names and table functions
    excluded), lowercased, in first-seen order."""
    ctes = set(cte_bodies(tree))
    out: list[str] = []
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            continue
        name = t.name.lower()
        if name in ctes or name in out:
            continue
        out.append(name)
    return out


def group_by_columns(tree: exp.Expression, table: Optional[str] = None) -> list[str]:
    """Plain column names in any GROUP BY (lowercased, qualifier dropped).
    Ordinals and expressions are skipped.

    With `table`: only the columns that can belong to it. A column qualified
    by ANOTHER physical table's name or alias is left out (`GROUP BY u.id`
    is not `events.id`); unqualified columns and those qualified by a
    subquery or CTE alias are kept, since resolving them needs the schema."""
    other: set[str] = set()
    if table is not None:
        mine = table_aliases(tree, table)
        other = {t.alias_or_name.lower() for t in tree.find_all(exp.Table)
                 if isinstance(t.this, exp.Identifier)} - mine
    cols: list[str] = []
    for g in tree.find_all(exp.Group):
        for e in g.expressions:
            if not isinstance(e, exp.Column) or e.name.lower() in cols:
                continue
            if e.table and e.table.lower() in other:
                continue
            cols.append(e.name.lower())
    return cols


def repeated_subqueries(tree: exp.Expression, min_len: int = 20) -> list[tuple[str, int]]:
    """Identical subqueries (by normalized SQL) that occur more than once."""
    counts: dict[str, int] = {}
    for sq in tree.find_all(exp.Subquery):
        if not isinstance(sq.this, (exp.Select, exp.SetOperation)):
            continue
        key = sq.this.sql(dialect=DIALECT).lower()
        if len(key) > min_len:
            counts[key] = counts.get(key, 0) + 1
    return [(k, n) for k, n in counts.items() if n > 1]


def star_tables(tree: exp.Expression) -> set[str]:
    """Physical tables whose columns a SELECT * (or alias.*) pulls directly.
    A star over a CTE or derived table does not count against the table
    underneath — the inner projection already pruned columns."""
    ctes = set(cte_bodies(tree))
    out: set[str] = set()
    for sel in tree.find_all(exp.Select):
        rels = {
            _rel_name(r): r.name.lower()
            for r, _ in _relations(sel)
            if isinstance(r, exp.Table)
            and isinstance(r.this, exp.Identifier)
            and r.name.lower() not in ctes
        }
        for e in sel.expressions:
            if isinstance(e, exp.Star):
                out.update(rels.values())
            elif isinstance(e, exp.Column) and isinstance(e.this, exp.Star):
                if e.table.lower() in rels:
                    out.add(rels[e.table.lower()])
    return out


def summed_columns(tree: exp.Expression) -> list[tuple[str, exp.Column]]:
    """(aggregate SQL, column) for every SUM/AVG — window forms included —
    whose argument is a bare column, possibly wrapped in parens or a cast.
    Expressions like SUM(a * b) are not included."""
    out = []
    for f in tree.find_all(exp.Sum, exp.Avg):
        arg = f.this
        while isinstance(arg, (exp.Paren, exp.Cast)):
            arg = arg.this
        if isinstance(arg, exp.Column) and not isinstance(arg.this, exp.Star):
            out.append((f.sql(dialect=DIALECT), arg))
    return out


def table_aliases(tree: exp.Expression, table: str) -> set[str]:
    """Names a physical table is referred to by in the query (alias or name)."""
    return {
        t.alias_or_name.lower()
        for t in tree.find_all(exp.Table)
        if isinstance(t.this, exp.Identifier) and t.name.lower() == table.lower()
    }


def redundant_distincts(tree: exp.Expression) -> list[str]:
    """SELECT DISTINCTs that provably cannot change the result:

    1. GROUP BY already makes rows unique — every grouped expression is in
       the select list (directly, by alias, or by ordinal);
    2. the select list is all aggregates with no GROUP BY — one row;
    3. duplicates are irrelevant where the SELECT sits: an IN (...) or
       EXISTS (...) subquery, or an arm of a deduplicating UNION.

    Skipped: DISTINCT ON, ROLLUP/CUBE/GROUPING SETS (subtotal rows can
    collide with real NULL keys), window functions.
    """
    out: list[str] = []
    for sel in tree.find_all(exp.Select):
        d = sel.args.get("distinct")
        if d is None or d.args.get("on") is not None or sel.find(exp.Window) is not None:
            continue
        parent = sel.parent
        if isinstance(parent, exp.Subquery) and isinstance(parent.parent, exp.In):
            out.append("SELECT DISTINCT inside IN (...) — membership ignores duplicates; drop DISTINCT")
            continue
        if isinstance(parent, exp.Exists):
            out.append("SELECT DISTINCT inside EXISTS (...) — existence ignores duplicates; drop DISTINCT")
            continue
        if isinstance(parent, exp.Union) and parent.args.get("distinct"):
            out.append("SELECT DISTINCT inside a UNION that already deduplicates; drop DISTINCT")
            continue
        group = sel.args.get("group")
        projections = sel.expressions
        if group is None:
            if projections and all(
                e.unalias().find(exp.AggFunc) is not None and not isinstance(e.unalias(), exp.Column)
                for e in projections
            ):
                out.append("SELECT DISTINCT over aggregates with no GROUP BY — the result is "
                           "one row; drop DISTINCT")
            continue
        if group.find(exp.Rollup, exp.Cube, exp.GroupingSets) is not None:
            continue
        proj_sql = {e.unalias().sql(dialect=DIALECT).lower() for e in projections}
        aliases = {e.alias.lower() for e in projections if e.alias}
        keys = group.expressions
        if keys and all(
            (isinstance(k, exp.Literal) and not k.is_string)  # GROUP BY 1, 2 -> in the list
            or k.sql(dialect=DIALECT).lower() in proj_sql
            or (isinstance(k, exp.Column) and not k.table and k.name.lower() in aliases)
            for k in keys
        ):
            cols = ", ".join(k.sql(dialect=DIALECT) for k in keys)
            out.append(f"SELECT DISTINCT is redundant: GROUP BY {cols} already makes every "
                       "row unique; drop DISTINCT (the engine may already skip it)")
    return list(dict.fromkeys(out))


def long_in_lists(tree: exp.Expression, max_items: int) -> list[tuple[str, int]]:
    """(column SQL, item count) for every IN (...) / NOT IN (...) with more
    than max_items explicit values. IN (subquery) is not a list."""
    out = []
    for node in tree.find_all(exp.In):
        n = len(node.expressions)
        if n > max_items:
            out.append((node.this.sql(dialect=current_dialect()), n))
    return out


def has_order_by(tree: exp.Expression) -> bool:
    """Top-level ORDER BY — decides whether result order is part of the answer."""
    return tree.args.get("order") is not None


# --------------------------------------------------------------------------- #
# Predicates
# --------------------------------------------------------------------------- #


def conjuncts(cond: Optional[exp.Expression]) -> list[exp.Expression]:
    if cond is None:
        return []
    if isinstance(cond, (exp.Where, exp.Having)):
        cond = cond.this
    return list(cond.flatten()) if isinstance(cond, exp.And) else [cond]


def _relations(select: exp.Select) -> list[tuple[exp.Expression, Optional[exp.Join]]]:
    from_ = select.args.get("from_")
    if from_ is None:
        return []
    rels = [(from_.this, None)]
    for j in select.args.get("joins") or []:
        rels.append((j.this, j))
    return rels


def _rel_name(rel: exp.Expression) -> str:
    return (rel.alias_or_name or "").lower()


def _is_one_row(body: Optional[exp.Expression]) -> bool:
    """A SELECT that provably returns at most one row: aggregates without
    GROUP BY, or LIMIT 1."""
    if not isinstance(body, exp.Select):
        return False
    limit = body.args.get("limit")
    if limit is not None and isinstance(limit.expression, exp.Literal):
        try:
            if int(limit.expression.this) <= 1:
                return True
        except ValueError:
            pass
    if body.args.get("group") is not None or not body.expressions:
        return False
    return all(
        e.find(exp.AggFunc) is not None or isinstance(e.unalias(), exp.Literal)
        for e in body.expressions
    )


def _is_tiny(rel: exp.Expression, ctes: dict, table_rows: dict[str, int]) -> bool:
    if isinstance(rel, exp.Subquery):
        return _is_one_row(rel.this)
    if isinstance(rel, exp.Values):
        return len(rel.expressions) <= TINY_RELATION_ROWS
    if isinstance(rel, exp.Table) and isinstance(rel.this, exp.Identifier):
        name = rel.name.lower()
        if name in ctes:
            return _is_one_row(ctes[name])
        rows = table_rows.get(name)
        return rows is not None and rows <= TINY_RELATION_ROWS
    return False


def cartesian_findings(
    tree: exp.Expression, table_rows: Optional[dict[str, int]] = None
) -> list[str]:
    """Relations in one FROM clause that no predicate links (F1.2).

    Per SELECT scope: union-find over its relations, linked by USING, by ON
    conjuncts, and by WHERE conjuncts that reference two relations. A
    conjunct that compares unqualified columns could link anything, so it
    links everything (no schema to resolve it — lenient on purpose; the
    estimate-based cap still catches real fan-out). Disconnected groups made
    only of provably tiny relations (one-row aggregates, LIMIT 1, tables at
    most TINY_RELATION_ROWS rows) are exempt.
    """
    table_rows = {k.lower(): v for k, v in (table_rows or {}).items()}
    ctes = cte_bodies(tree)
    findings: list[str] = []
    for select in tree.find_all(exp.Select):
        rels = _relations(select)
        if len(rels) < 2:
            continue
        names = [_rel_name(r) for r, _ in rels]
        parent = list(range(len(rels)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            parent[find(i)] = find(j)

        def union_all():
            for i in range(1, len(rels)):
                union(0, i)

        preds: list[exp.Expression] = conjuncts(select.args.get("where"))
        explicit_cross = False
        for idx, (rel, join) in enumerate(rels):
            if isinstance(rel, (exp.Unnest, exp.Lateral)):
                union_all()  # correlated by construction
            if join is None:
                continue
            if (join.args.get("kind") or "").upper() == "CROSS":
                explicit_cross = True
            if join.args.get("using"):
                for k in range(idx):
                    union(idx, k)
            preds.extend(conjuncts(join.args.get("on")))

        for p in preds:
            cols = list(p.find_all(exp.Column))
            idxs = {names.index(c.table.lower()) for c in cols
                    if c.table and c.table.lower() in names}
            unqualified = [c for c in cols if not c.table]
            if len(idxs) >= 2:
                it = iter(idxs)
                first = next(it)
                for k in it:
                    union(first, k)
            elif unqualified and len(cols) >= 2:
                union_all()

        groups: dict[int, list[int]] = {}
        for i in range(len(rels)):
            groups.setdefault(find(i), []).append(i)
        big = [
            g for g in groups.values()
            if not all(_is_tiny(rels[i][0], ctes, table_rows) for i in g)
        ]
        if len(groups) > 1 and len(big) > 1:
            parts = [" + ".join(names[i] for i in g) for g in big]
            # The BigQuery parser stores a comma join as kind CROSS too.
            kind = ("join" if not explicit_cross else "CROSS JOIN or comma join"
                    if current_dialect() == "bigquery" else "explicit CROSS JOIN")
            findings.append(
                f"cartesian product: {kind} with no predicate linking "
                f"{' and '.join(repr(p) for p in parts)}"
            )
    return findings


_COMPARISONS = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.NEQ)


@dataclass
class PartitionFilterUse:
    bare: list[str]  # predicates comparing the bare partition column
    wrapped: list[str]  # predicates where the column sits inside a function/cast/arithmetic
    # Per wrapped predicate: what wraps the column, innermost first, e.g.
    # ("date", "timestamp_trunc") — see wrapper_chain.
    wrapped_chains: list[tuple[str, ...]] = field(default_factory=list)


def wrapper_chain(col: exp.Column, stop: exp.Expression) -> tuple[str, ...]:
    """What wraps `col` up to `stop`, innermost first: "cast:<type>" for casts,
    the function name for functions ("format_timestamp:<fmt>" with its format
    literal), "+interval"/"-interval" or the operator name for arithmetic.
    Parentheses are skipped."""
    out, node = [], col.parent
    while node is not None and node is not stop:
        if isinstance(node, exp.Cast):
            out.append("cast:" + node.to.sql(dialect=DIALECT).lower())
        elif isinstance(node, (exp.TsOrDsToTimestamp, exp.TsOrDsToDate)) and isinstance(
                node.parent, exp.TimeToStr):
            pass  # BigQuery parser's implicit coercion inside FORMAT_TIMESTAMP/FORMAT_DATE
        elif isinstance(node, exp.TimeToStr):
            # BigQuery FORMAT_TIMESTAMP / FORMAT_DATE, as the BigQuery parser builds them
            fmt = node.args.get("format")
            kind = "format_date" if isinstance(node.this, exp.TsOrDsToDate) else "format_timestamp"
            out.append(kind + ":" + (fmt.this if isinstance(fmt, exp.Literal) else "?"))
        elif isinstance(node, exp.Func):
            name = _func_name(node)
            if name in ("format_timestamp", "format_date"):
                fmt = node.expressions[0] if isinstance(node, exp.Anonymous) and node.expressions else None
                name += ":" + (fmt.this if isinstance(fmt, exp.Literal) else "?")
            out.append(name)
        elif isinstance(node, (exp.Add, exp.Sub)):
            sign = "+" if isinstance(node, exp.Add) else "-"
            out.append(sign + ("interval" if node.find(exp.Interval) else "arith"))
        elif not isinstance(node, exp.Paren):
            out.append(type(node).__name__.lower())
        node = node.parent
    return tuple(out)


def partition_filter_use(tree: exp.Expression, table: str, column: str) -> PartitionFilterUse:
    """How `table.column` is filtered anywhere in the query (WHERE and ON).

    A column reference counts for `table` when it is unqualified, qualified
    by the table's own name/alias, or qualified by a derived table/CTE (the
    optimizer can push those down). Only a qualifier naming a different
    physical table excludes it.
    """
    table, column = table.lower(), column.lower()
    own, other = set(), set()
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            continue
        (own if t.name.lower() == table else other).add(t.alias_or_name.lower())
    other -= own
    if not own:
        return PartitionFilterUse([], [], [])

    clauses: list[exp.Expression] = list(tree.find_all(exp.Where))
    clauses += [j.args["on"] for j in tree.find_all(exp.Join) if j.args.get("on")]
    bare, wrapped, chains = [], [], {}
    for clause in clauses:
        for cmp in clause.find_all(*_COMPARISONS):
            for col in cmp.find_all(exp.Column):
                if col.name.lower() != column:
                    continue
                if col.table and col.table.lower() in other:
                    continue
                node, is_bare = col.parent, True
                while node is not None and node is not cmp:
                    if not isinstance(node, exp.Paren):
                        is_bare = False
                        break
                    node = node.parent
                text = cmp.sql(dialect=DIALECT)
                if is_bare:
                    bare.append(text)
                else:
                    wrapped.append(text)
                    chains.setdefault(text, wrapper_chain(col, cmp))
    wrapped = list(dict.fromkeys(wrapped))
    return PartitionFilterUse(list(dict.fromkeys(bare)), wrapped, [chains[w] for w in wrapped])


def filters_after_join(tree: exp.Expression) -> list[str]:
    """Filters applied after a join or aggregation instead of before it:

    1. an outer WHERE that filters a derived table / CTE whose body joins —
       the filter runs over the joined rows (the optimizer may push it down;
       the pattern is still reported, per the eval's optimizer_may_fix cases);
    2. a HAVING conjunct with no aggregate — it belongs in WHERE.
    """
    ctes = cte_bodies(tree)
    out: list[str] = []
    for select in tree.find_all(exp.Select):
        rels = _relations(select)
        for rel, _ in rels:
            body = None
            if isinstance(rel, exp.Subquery):
                body = rel.this
            elif isinstance(rel, exp.Table) and rel.name.lower() in ctes:
                body = ctes[rel.name.lower()]
            if not isinstance(body, exp.Select) or not body.args.get("joins"):
                continue
            alias = _rel_name(rel)
            for p in conjuncts(select.args.get("where")):
                cols = list(p.find_all(exp.Column))
                hits = [c for c in cols if (c.table or "").lower() == alias
                        or (not c.table and len(rels) == 1)]
                if hits:
                    out.append(
                        f"filter {p.sql(dialect=current_dialect())!r} applied after the join "
                        f"inside '{alias}' — filter before joining"
                    )
        for p in conjuncts(select.args.get("having")):
            if p.find(exp.AggFunc) is None:
                out.append(
                    f"HAVING {p.sql(dialect=current_dialect())!r} has no aggregate — "
                    "move it to WHERE so rows drop before grouping"
                )
    return list(dict.fromkeys(out))
