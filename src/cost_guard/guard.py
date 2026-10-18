"""Guard: pre-execution decision — allow / warn / rewrite / block.

The gate is deterministic: read-only check, cost estimate, static rules,
hard caps from customer.yaml for the query's workload class. An LLM, when one
is configured, runs only AFTER that verdict exists and only on flagged
queries: it may propose a rewrite (offered only if compare_results verifies
it) and explain the flag in plain language. It never changes allow/block.

warn = allow + annotate the log. A "rewrite" verdict means a cheaper
alternative was generated and verified — the analyst's original query still
runs; the verified alternative is offered alongside it. There is no human
approval workflow in this POC; a cost block can be bypassed only when
customer.yaml sets guard.allow_bypass AND the caller passes a reason, and the
bypass is traced. Non-read-only statements are never bypassable.

Every decision emits a trace (estimate + per-rule spans) via the tracer
module, including which rules did not run and why.
"""
from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Collection, Literal, Optional, Sequence

from . import sql_ast
from .thresholds import fingerprint_query

# Warehouse calls that don't depend on each other run at the same time: the
# query's dry run, each table's metadata, and the join check's dry run. On
# BigQuery each is a network round trip (~0.2-0.9 s); one after another they
# added up (eval: a dated join took 3.8 s before caching and overlap).
_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="cost-guard")

# Rewrite verification runs real (small) queries — ~8-10 s on BigQuery. A
# long-running service can cap how long a check waits for it
# (decide(rewrite_wait_sec=...), customer.yaml guard.rewrite_wait_sec): past
# the limit the check answers without the rewrite and verification finishes
# in the background. Results — passed AND failed — are remembered in this
# process only (keyed by a hash of query + rewrite; nothing on disk), so the
# same query checked again gets its verified rewrite at once and a failed one
# is not re-run. A one-shot CLI process has no "later": it waits.
# SELECT * (owner's call, 2026-10-17): flagged on cost, not column count.
SELECT_STAR_COST_SHARE = 0.5  # estimate >= half the class cap (bytes on BigQuery)
SELECT_STAR_RUNTIME_SHARE = 0.5  # or median runtime >= half the class runtime limit
SELECT_STAR_MIN_RUNS = 20  # runs of history before the runtime half applies (= anomaly cold start)

VERIFY_MEMORY_TTL_SEC = 24 * 3600
VERIFY_MEMORY_MAX = 10_000
_VERIFY_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cost-guard-verify")
_verify_lock = threading.Lock()
_verified: dict = {}  # key -> (expires_at, ComparisonOutcome)
_pending: dict = {}  # key -> Future


def clear_verification_memory() -> None:
    with _verify_lock:
        _verified.clear()
        _pending.clear()


def _backend_key(backend) -> tuple:
    return tuple(str(getattr(backend, a, "")) for a in ("name", "project", "dataset", "db_path"))


def _verify(sql: str, cand: str, backend, wait_sec: Optional[float]):
    """(outcome, how): how is "verified" (now), "remembered" (earlier), or
    "pending" (outcome None: still running past wait_sec)."""
    from .tools.compare_results import compare_results

    norm = lambda q: " ".join(q.split())  # noqa: E731
    key = (_backend_key(backend), hashlib.sha256(f"{norm(sql)}\0{norm(cand)}".encode()).hexdigest())
    dialect = sql_ast.current_dialect()

    def run():
        try:
            with sql_ast.using_dialect(dialect):
                res = compare_results(sql, cand, backend=backend)
            with _verify_lock:
                if len(_verified) >= VERIFY_MEMORY_MAX:  # drop the entry closest to expiry
                    _verified.pop(min(_verified, key=lambda k: _verified[k][0]))
                _verified[key] = (time.monotonic() + VERIFY_MEMORY_TTL_SEC, res)
            return res
        finally:
            with _verify_lock:
                _pending.pop(key, None)

    with _verify_lock:
        hit = _verified.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1], "remembered"
        fut = _pending.get(key)
        if fut is None:
            fut = _pending[key] = _VERIFY_POOL.submit(run)
    try:
        return fut.result(timeout=wait_sec), "verified"
    except FutureTimeout:
        return None, "pending"

Decision = Literal["allow", "warn", "rewrite", "block"]


@dataclass
class GuardVerdict:
    decision: Decision
    reason: str
    estimated_bytes: Optional[int] = None
    estimated_runtime_sec: Optional[float] = None  # None = unknown (cold fingerprint)
    rewritten_sql: Optional[str] = None
    findings: list[str] = field(default_factory=list)
    # Rules that did not run for this query, with the reason. Empty means
    # full coverage — never silently partial.
    skipped_rules: list[str] = field(default_factory=list)
    estimated_rows: Optional[int] = None  # rows processed (scan or peak), DuckDB proxy
    estimate_source: Optional[str] = None  # "explain" | "dry_run" | "table_sum_fallback"
    workload_class: str = "interactive"
    caps: dict = field(default_factory=dict)
    rewrite_source: Optional[str] = None  # "deterministic:cte" | "llm"
    verification: Optional[str] = None  # compare_results detail for the offered rewrite
    explanation: Optional[str] = None  # plain-language LLM explanation (warn/block only)
    bypassed: Optional[str] = None  # bypass reason when an escape hatch was used
    # Blocks only: the main cause first, then fixes, each marked whether it
    # changes the result (fix_hints.py). Deterministic; never changes a decision.
    fix_hints: list[dict] = field(default_factory=list)


def is_read_only(sql: str) -> bool:
    """Deterministic classifier (AST): exactly one SELECT/WITH statement, no
    write nodes, no file/network/environment functions. Fails closed on parse
    errors. Only read-only queries may be auto-killed by the runtime watchdog.
    """
    return sql_ast.is_read_only(sql)


def decide(
    sql: str,
    workload_class: str = "interactive",
    customer: Optional[dict] = None,
    backend=None,
    tracer=None,
    llm=None,
    bypass_reason: Optional[str] = None,
    offered_rewrites: Optional[Collection[str]] = None,
    rewrite_wait_sec: Optional[float] = None,
    shape_runtimes: Optional[Sequence[float]] = None,
    shape_volume=None,
) -> GuardVerdict:
    """Guard pipeline: read-only check -> cost estimate -> static rules
    (per referenced table, with column profiles) -> hard caps for the
    workload class -> verdict -> (optional) LLM rewrite + explanation.

    customer: parsed customer.yaml; loaded from the repo root when None.
    offered_rewrites: fingerprints of rewrites the Guard offered earlier (the
    query log's rewrite_fingerprint). A query with one of those shapes IS an
    offered rewrite being run, so it gets no second rewrite: one round per
    query. The catalog fixers already stop after one round (a fixed query no
    longer trips the rule); this bounds the LLM proposer, which could keep
    offering "improvements" to its own output.
    rewrite_wait_sec: how long to wait for a rewrite's verification; None
    (default) waits until it is done. For long-running services, see
    _verify (customer.yaml guard.rewrite_wait_sec).
    shape_runtimes: run times (seconds) of this query shape's completed runs,
    from the query log; used by the SELECT * check (see _select_star_finding).
    shape_volume: runtime.ShapeVolume, this shape's recent executed runs from
    the query log; used by the volume rule (see _volume_finding).
    """
    from .config import load_customer
    from .tracing import get_tracer

    customer = customer if customer is not None else load_customer()
    tracer = tracer or get_tracer()
    dialect = "bigquery" if getattr(backend, "name", None) == "bigquery" else sql_ast.DIALECT
    with tracer.trace("guard.decide", workload_class=workload_class) as tid, \
            sql_ast.using_dialect(dialect):
        ctx: dict = {}
        verdict = _decide(sql, workload_class, customer, backend, tracer, tid, llm,
                           offered_rewrites, rewrite_wait_sec, ctx, shape_runtimes, shape_volume)
        if verdict.decision == "block":
            _add_fix_hints(sql, verdict, customer, backend, tracer, tid, ctx)
        verdict = _maybe_bypass(verdict, customer, bypass_reason, tracer, tid)
        if llm is not None and verdict.decision in ("warn", "block") and verdict.findings:
            with tracer.span(tid, "llm.explain_flag") as span:
                try:
                    verdict.explanation = llm.explain_flag(sql, verdict.findings)
                except Exception as e:  # noqa: BLE001 — explanation is optional
                    span["attrs"]["error"] = str(e)[:200]
        return verdict


def _maybe_bypass(verdict, customer, bypass_reason, tracer, tid) -> GuardVerdict:
    from .config import bypass_allowed

    if verdict.decision != "block" or not bypass_reason:
        return verdict
    if "non-read-only statement" in verdict.findings:
        return verdict  # never bypassable
    with tracer.span(tid, "guard.bypass", reason=bypass_reason,
                     allowed=bypass_allowed(customer)):
        pass
    if not bypass_allowed(customer):
        verdict.reason += " (bypass requested but guard.allow_bypass is false)"
        return verdict
    verdict.decision = "warn"
    verdict.bypassed = bypass_reason
    verdict.reason = f"BYPASSED ({bypass_reason}) — would have blocked: {verdict.reason}"
    return verdict


def _add_fix_hints(sql, verdict, customer, backend, tracer, tid, ctx) -> None:
    """Blocked verdicts get ranked causes and fixes (fix_hints.py), from the
    metadata the decision already fetched. A failure leaves no hints, traced."""
    from .fix_hints import fix_hints

    with tracer.span(tid, "fix_hints") as span:
        try:
            registry = ctx.get("registry")
            if registry is None and backend is not None and ctx.get("table_md"):
                get = getattr(backend, "column_registry", None)
                registry = get() if get is not None else None
            ests = ctx.get("join_ests")
            if ests is None and registry is not None and len(ctx.get("table_md") or {}) > 1:
                from .join_estimate import join_estimates

                ests = join_estimates(ctx["tree"], registry, ctx["max_age_days"])
            verdict.fix_hints = fix_hints(sql, verdict, ctx.get("table_md") or {}, registry,
                                          customer, getattr(backend, "name", None), ests)
            span["attrs"]["hints"] = len(verdict.fix_hints)
        except Exception as e:  # noqa: BLE001 — hints are advice; the verdict stands
            span["attrs"]["error"] = str(e)[:200]


def _select_star_finding(tree, estimate, caps, workload_class,
                         shape_runtimes) -> Optional[str]:
    """SELECT * is judged on what it costs, not on how many columns the table
    has (owner's call, 2026-10-17): it is flagged when the estimate is at
    least SELECT_STAR_COST_SHARE of the class cap (BigQuery: dry-run bytes vs
    the bytes cap; DuckDB: rows vs the rows cap), or when the shape's median
    runtime over at least SELECT_STAR_MIN_RUNS completed runs is at least
    SELECT_STAR_RUNTIME_SHARE of the class runtime limit. Nothing predicts a
    runtime before a run (no EXPLAIN on BigQuery; a dry run has no time), so
    the runtime half needs the shape's own history and is silent without it."""
    import statistics

    stars = sorted(sql_ast.star_tables(tree))
    if not stars:
        return None
    what = f"SELECT * on {', '.join(stars)}"
    fix = " — list only the columns you need"
    if estimate is not None:
        if estimate.source == "dry_run":
            used, cap, unit = estimate.estimated_bytes_scanned, caps.get("max_bytes_scanned"), "bytes"
        else:
            used, cap, unit = estimate.rows_processed, caps.get("max_rows_processed"), "rows"
        if used and cap and used >= SELECT_STAR_COST_SHARE * cap:
            return (f"{what} reads ~{used:,} {unit}, {used / cap:.0%} of the {workload_class} "
                    f"cap of {cap:,}{fix}")
    limit = caps.get("max_runtime_sec")
    runs = [d for d in (shape_runtimes or []) if d is not None]
    if limit and len(runs) >= SELECT_STAR_MIN_RUNS:
        median = statistics.median(runs)
        if median >= SELECT_STAR_RUNTIME_SHARE * limit:
            return (f"{what} usually runs {median:.1f} s (median of {len(runs)} runs), "
                    f"{median / limit:.0%} of the {workload_class} limit of {limit:g} s{fix}")
    return None


def _volume_finding(estimate, tables, volume, customer, backend):
    """Volume rule (owner's priority 4: spend = cost per run x runs). Warns,
    never blocks, when a shape ran at least `min_runs` times in the window
    and its modeled total, this run included, reaches `warn_usd_per_window`
    (customer.yaml `volume`). Each query may be cheap; the total isn't.
    This run counts its dry-run bytes, raised to BigQuery's 10 MiB minimum
    per table; logged runs count what they billed (a cache hit bills 0).
    Returns (finding, None), (None, why skipped) or (None, None)."""
    from .config import volume_settings
    from .runtime import bigquery_billed

    cfg = volume_settings(customer)
    price = ((customer or {}).get("cost_model") or {}).get("price_per_tb_usd")
    est = estimate.estimated_bytes_scanned if estimate is not None else None
    if getattr(backend, "name", None) != "bigquery" or est is None or not price:
        return None, "needs BigQuery dry-run bytes and cost_model.price_per_tb_usd"
    runs = volume.runs + 1
    if runs < cfg["min_runs"]:
        return None, None
    this_run = bigquery_billed(est, len(tables))
    usd = (volume.bytes + this_run) / 1024**4 * price
    if usd < cfg["warn_usd_per_window"]:
        return None, None
    return (f"this query shape ran {runs:,} times in the last {volume.window_hours:g} h, "
            f"~${usd:,.2f} billed in total (MODELED; ~${this_run / 1024**4 * price:,.4f} this run) — "
            "at this volume, read from a summary table or materialized view, or run it "
            "once on a schedule and share the result"), None


def _decide(sql, workload_class, customer, backend, tracer, tid, llm,
            offered_rewrites=None, rewrite_wait_sec=None, ctx=None,
            shape_runtimes=None, shape_volume=None) -> GuardVerdict:
    from .column_registry import registry_settings
    from .config import caps_for, partition_column
    from .rewrites import PRUNING_ONLY_FIXERS, deterministic_rewrite
    from .rules.static_rules import ALL_RULES, LARGE_TABLE_ROWS, QUERY_RULES

    caps = caps_for(customer, workload_class)
    base = dict(workload_class=workload_class, caps=caps)

    violation = sql_ast.read_only_violation(sql)
    if violation:
        return GuardVerdict(
            decision="block",
            reason=f"not a read-only SELECT/WITH query ({violation}) — "
                   "the Guard only auto-handles reads",
            findings=["non-read-only statement"],
            skipped_rules=[f"{r.__name__}: query is not read-only" for r in ALL_RULES],
            **base,
        )
    tree = sql_ast.parse_one(sql)
    # Started now, waited for below: the dry run / EXPLAIN runs while the
    # table metadata is fetched (in parallel too) and the rules are prepared.
    dialect = sql_ast.current_dialect()

    def _in_dialect(fn, *args):
        with sql_ast.using_dialect(dialect):  # context variables don't follow threads
            return fn(*args)

    est_future = _POOL.submit(_in_dialect, backend.estimate, sql) if backend is not None else None

    tables = sql_ast.referenced_tables(tree)
    md_futures = ({t: _POOL.submit(_in_dialect, backend.table_metadata, t) for t in tables}
                  if backend is not None else {})
    group_cols = sql_ast.group_by_columns(tree)
    table_md: dict[str, dict] = {}
    no_md: dict[str, str] = {}
    no_profile: list[str] = []
    max_age_days = registry_settings(customer)["max_age_days"]
    registry = None
    not_in = sql_ast.not_in_subqueries(tree)
    if backend is not None and tables and (group_cols or sql_ast.summed_columns(tree) or not_in):
        with tracer.span(tid, "column_registry.lookup"):
            get = getattr(backend, "column_registry", None)
            try:
                registry = get() if get is not None else None
            except Exception:  # noqa: BLE001 — missing registry = visible skip
                registry = None
    def _has_column(table: str, column: str) -> bool:
        """Does `table` have `column` (from its metadata)? For resolving an
        unqualified GROUP BY column in a join to its own table."""
        try:
            fut = md_futures.get(table)
            return fut is not None and column in [c.lower() for c in fut.result().get("columns", [])]
        except Exception:  # noqa: BLE001 — no metadata: can't tell
            return False

    for t in tables:
        if backend is None:
            no_md[t] = "no backend"
            continue
        try:
            md = dict(md_futures[t].result())
        except Exception as e:  # noqa: BLE001
            no_md[t] = f"metadata unavailable ({str(e).splitlines()[0][:60]})"
            continue
        md["table"] = t
        md["engine"] = getattr(backend, "name", None)  # warehouse-aware rules
        if md.get("num_rows") is None and registry is not None:
            # A view reports no row count; the registry profiled it through the
            # view. Without this a 100k-row view is never "large" and the
            # GROUP BY rule skips it (P32).
            entry = next((e for e in registry.entries() if e.table_name.lower() == t.lower()), None)
            if entry is not None and entry.age_days() <= max_age_days:
                md["num_rows"] = int(entry.total_rows)
        md["large"] = (md.get("num_rows") or 0) >= LARGE_TABLE_ROWS
        # The warehouse's own partitioning wins; customer.yaml is the fallback
        # for engines without real partitions (DuckDB: the zone-mapped date column).
        declared = partition_column(customer, t)
        actual = md.get("partitioned_by")
        md["partition_column"] = actual or declared
        md["partition_source"] = ("warehouse" if actual
                                  else "customer.yaml" if declared else None)
        if actual and declared and actual.lower() != declared.lower():
            with tracer.span(tid, "partition_column.mismatch", table=t,
                             warehouse=actual, customer_yaml=declared):
                pass
        # Column profiles for grouped columns come from the column registry
        # (profiled once, refreshed on a schedule) — a lookup, never a scan.
        profiles = {}
        table_cols = [c.lower() for c in md.get("columns", [])]
        for col in sql_ast.group_by_columns(tree, t, has_column=_has_column):
            if col not in table_cols:
                continue
            entry = registry.lookup(t, col) if registry is not None else None
            if entry is not None and entry.age_days() <= max_age_days:
                profiles[col] = entry.as_profile()
            elif md["large"]:  # the rule only applies to large tables
                if registry is None:
                    why = "no column registry for this warehouse"
                elif entry is None:
                    why = "column not in the registry"
                else:  # stale entries are unverified, not trusted silently
                    why = (f"registry entry is {entry.age_days():.0f} days old "
                           f"(> {max_age_days}) — unverified")
                no_profile.append(f"no_high_cardinality_groupby on '{t}.{col}': {why}"
                                  " — run scripts/build_column_registry.py")
        md["column_profiles"] = profiles
        # NULL counts of NOT IN subquery columns read from this table.
        md["not_in_columns"] = {}
        for nt, col, _ in not_in:
            if nt != t.lower():
                continue
            entry = registry.lookup(t, col) if registry is not None else None
            if entry is not None and entry.age_days() <= max_age_days:
                md["not_in_columns"][col] = entry
            else:
                why = ("no column registry for this warehouse" if registry is None
                       else "column not in the registry" if entry is None
                       else f"registry entry is {entry.age_days():.0f} days old (> {max_age_days})")
                no_profile.append(f"no_not_in_over_nulls on '{t}.{col}': {why}")
        md["registry"] = registry
        try:
            md["column_kinds"] = backend.column_kinds() if hasattr(backend, "column_kinds") else {}
        except Exception:  # noqa: BLE001 — no labels: the name rules decide
            md["column_kinds"] = {}
        md["registry_max_age_days"] = max_age_days
        table_md[t] = md
    table_rows = {t: md.get("num_rows") for t, md in table_md.items()}
    # A view reports no row count; the registry profiled it through the view.
    # Without this a 10-row view never counts as tiny for the cartesian rule (T27).
    if registry is None and backend is not None and None in table_rows.values():
        with tracer.span(tid, "column_registry.lookup"):
            get = getattr(backend, "column_registry", None)
            try:
                registry = get() if get is not None else None
            except Exception:  # noqa: BLE001 — no registry: the row count stays unknown
                registry = None
    if registry is not None:
        for t, rows in table_rows.items():
            if rows is None:
                entry = next((e for e in registry.entries() if e.table_name.lower() == t.lower()), None)
                if entry is not None and entry.age_days() <= max_age_days:
                    table_rows[t] = int(entry.total_rows)
    if ctx is not None:  # for fix hints on a block (decide -> _add_fix_hints)
        ctx.update(tree=tree, table_md=table_md, registry=registry, max_age_days=max_age_days)

    # BigQuery: the join check needs its own dry run (the share of partitions a
    # date filter reads); it doesn't need the main estimate, so start it now.
    join_future = None
    if backend is not None and hasattr(backend, "partition_fraction") and caps.get("max_rows_processed"):
        from .join_estimate import join_estimates

        join_future = _POOL.submit(
            _in_dialect, join_estimates, tree, backend.column_registry(), max_age_days,
            {t: md.get("partition_column") for t, md in table_md.items()},
            getattr(backend, "partition_fraction", None))

    with tracer.span(tid, "estimate") as span:  # time here = waiting, the call overlapped
        estimate = est_future.result() if est_future is not None else None
        if estimate is not None:
            span["attrs"].update(source=estimate.source, rows=estimate.rows_processed)
    rows = estimate.rows_processed if estimate else 0
    base.update(
        estimated_rows=rows if estimate else None,
        estimated_bytes=estimate.estimated_bytes_scanned if estimate else None,
        estimate_source=estimate.source if estimate else None,
    )

    findings: list[str] = []
    skipped: list[str] = []

    def run(rule, md, label):
        with tracer.span(tid, f"rule.{rule.__name__}", table=md.get("table")) as span:
            try:
                found = rule(sql, md)
                findings.extend(found)
                span["attrs"]["findings"] = len(found)
            except NotImplementedError:
                skipped.append(f"{label}: not implemented")
            except Exception as e:  # noqa: BLE001 — a broken rule is visible, not fatal
                skipped.append(f"{label}: error ({type(e).__name__}: {str(e)[:80]})")

    for rule in ALL_RULES:
        if rule in QUERY_RULES:
            run(rule, {"table": None, "table_rows": table_rows,
                       "engine": getattr(backend, "name", None)}, rule.__name__)
            continue
        for t, md in table_md.items():
            run(rule, {**md, "table_rows": table_rows}, f"{rule.__name__} on '{t}'")
        for t, why in no_md.items():
            skipped.append(f"{rule.__name__} on '{t}': {why}")
    # Plan red flag: a full in-memory sort of a large input (ORDER BY with no
    # LIMIT). Read from the EXPLAIN plan, not the SQL text — a LIMIT turns
    # the sort into a TOP_N, and a sort after GROUP BY sorts only the groups.
    sorted_rows = estimate.estimated_sorted_rows if estimate is not None else None
    if sorted_rows is None:
        why = estimate.source if estimate is not None else "no backend"
        skipped.append(f"plan_unbounded_sort: plan shape not visible ({why})")
    elif sorted_rows >= LARGE_TABLE_ROWS:
        with tracer.span(tid, "plan.unbounded_sort", rows=sorted_rows):
            findings.append(
                f"full in-memory sort of ~{sorted_rows:,} rows (ORDER BY with no LIMIT) — "
                "add a LIMIT, or drop the ORDER BY if the consumer re-sorts"
            )
    star = _select_star_finding(tree, estimate, caps, workload_class, shape_runtimes)
    if star:
        with tracer.span(tid, "select_star.costly"):
            findings.append(star)
    if shape_volume is not None:
        volume, why = _volume_finding(estimate, tables, shape_volume, customer, backend)
        if volume:
            with tracer.span(tid, "volume.costly", runs=shape_volume.runs + 1):
                findings.append(volume)
        elif why:
            skipped.append(f"volume_cost: {why}")
    findings = list(dict.fromkeys(findings))  # same finding can fire per table
    skipped.extend(no_profile)
    if skipped:
        with tracer.span(tid, "rules.skipped", rules=skipped):
            pass
    base["skipped_rules"] = skipped

    max_rows = caps.get("max_rows_processed")
    if estimate is not None and max_rows and rows > max_rows:
        return GuardVerdict(
            decision="block",
            reason=(
                f"estimated {rows:,} rows processed ({estimate.source}) exceeds the "
                f"{workload_class} hard cap of {max_rows:,} — hard caps never adapt"
            ),
            findings=findings,
            **base,
        )
    max_bytes = caps.get("max_bytes_scanned")
    est_bytes = base["estimated_bytes"]
    if max_bytes and est_bytes and est_bytes > max_bytes:
        return GuardVerdict(
            decision="block",
            reason=(
                f"estimated {est_bytes:,} bytes scanned exceeds the {workload_class} "
                f"hard cap of {max_bytes:,} — hard caps never adapt"
            ),
            findings=findings,
            **base,
        )

    if estimate is not None and estimate.source == "dry_run" and max_rows:
        # A dry run reports bytes read, not rows: a join that multiplies rows
        # is invisible to it. Estimate join output from the registry instead.
        from .join_estimate import FANOUT_MIN_RATIO, join_estimates

        with tracer.span(tid, "join_estimate") as span:
            try:
                ests = (join_future.result() if join_future is not None else join_estimates(
                    tree, backend.column_registry(), max_age_days,
                    partition_columns={t: md.get("partition_column") for t, md in table_md.items()},
                    partition_fraction=getattr(backend, "partition_fraction", None)))
            except Exception as e:  # noqa: BLE001 — no estimate = no join check
                ests, span["attrs"]["error"] = [], str(e)[:200]
            span["attrs"]["estimates"] = [
                dict(rows=e.rows, fanout=round(e.fanout, 2), filtered=e.filtered, keys=e.keys)
                for e in ests]
        if ctx is not None:
            ctx["join_ests"] = ests
        fanouts = [e for e in ests if not e.filtered and e.fanout >= FANOUT_MIN_RATIO
                   and e.rows > max_rows]
        if fanouts:
            worst = max(fanouts, key=lambda e: e.rows)
            return GuardVerdict(
                decision="block",
                reason=(
                    f"join fan-out: estimated ~{worst.rows:,} rows from joining "
                    f"{' + '.join(worst.tables)} on {', '.join(worst.keys)} "
                    f"(x{worst.fanout:.1f} the largest input; column registry) exceeds the "
                    f"{workload_class} hard cap of {max_rows:,} rows — hard caps never adapt"
                ),
                findings=findings,
                **base,
            )

    if any(f.lower().startswith("cartesian") for f in findings):
        return GuardVerdict(
            decision="block",
            reason="runaway join pattern: " + "; ".join(findings),
            findings=findings,
            **base,
        )

    from .rules.static_rules import IDENTIFIER_AGGREGATE

    id_aggs = [f for f in findings if f.startswith(IDENTIFIER_AGGREGATE)]
    if id_aggs:
        return GuardVerdict(
            decision="block",
            reason="meaningless result — " + "; ".join(id_aggs),
            findings=findings,
            **base,
        )

    if findings and offered_rewrites and fingerprint_query(sql) in offered_rewrites:
        with tracer.span(tid, "rewrite.already_offered"):
            pass
        findings.append("this query is a rewrite the Guard already offered — "
                        "no second rewrite (one round per query)")
    elif findings and backend is not None:
        # Deterministic fixers first (chained); then the LLM proposer if
        # configured. Either way an unverified rewrite is never offered.
        candidates = []
        with tracer.span(tid, "suggest_rewrite") as span:
            cand, applied = deterministic_rewrite(sql, findings)
            span["attrs"]["fixers"] = applied
        if cand:
            candidates.append(("deterministic:" + "+".join(applied), cand))
        pruning_only = bool(applied) and set(applied) <= PRUNING_ONLY_FIXERS
        if not candidates and llm is not None:
            with tracer.span(tid, "llm.propose_rewrite") as span:
                try:
                    prop = llm.propose_rewrite(sql, findings)
                except Exception as e:  # noqa: BLE001 — LLM failure = no rewrite
                    prop = None
                    span["attrs"]["error"] = str(e)[:200]
            if prop is not None and prop.rewritten_sql:
                candidates.append(("llm", prop.rewritten_sql))
        for source, cand in candidates:
            if " ".join(cand.split()).lower() == " ".join(sql.split()).lower():
                continue
            if sql_ast.read_only_violation(cand):
                findings.append(f"{source} rewrite is not read-only — not offered")
                continue
            if estimate is not None and estimate.source == "dry_run":
                # BigQuery: dry-run bytes are exact, so a rewrite must bill FEWER
                # bytes — "not costlier" would offer ones that save nothing
                # (CTE/pushdown rewrites measured 0 bytes saved there). Billed,
                # not read: BigQuery bills at least 10 MiB per table, so on a
                # small table a rewrite that reads less saves nothing, and its
                # sampled verification (~30 MiB billed) would cost more (T15).
                try:
                    cand_bytes = backend.estimate(cand).estimated_bytes_scanned or 0
                except Exception as e:  # noqa: BLE001 — the warehouse rejected it: not offered
                    findings.append(f"{source} rewrite could not be estimated — not offered "
                                    f"({str(e).splitlines()[0][:120]})")
                    continue
                from .runtime import bigquery_billed

                cand_billed = bigquery_billed(cand_bytes, len(sql_ast.referenced_tables(
                    sql_ast.parse_one(cand))))
                orig_billed = bigquery_billed(base["estimated_bytes"] or 0, len(tables))
                if cand_billed >= orig_billed:
                    findings.append(f"{source} rewrite bills no fewer bytes ({cand_billed:,} vs "
                                    f"{orig_billed:,}, BigQuery's 10 MiB-per-table minimum included)"
                                    " — not offered")
                    continue
            elif estimate is not None and not (source.startswith("deterministic:") and pruning_only):
                try:
                    cand_est = backend.estimate(cand)
                except Exception as e:  # noqa: BLE001 — the warehouse rejected it: not offered
                    findings.append(f"{source} rewrite could not be estimated — not offered "
                                    f"({str(e).splitlines()[0][:120]})")
                    continue
                if cand_est.rows_processed > rows:
                    findings.append(f"{source} rewrite is not cheaper by estimate — not offered")
                    continue
            with tracer.span(tid, "compare_results", source=source) as span:
                res, how = _verify(sql, cand, backend, rewrite_wait_sec)
                span["attrs"]["how"] = how
            if res is None:
                findings.append(
                    f"{source} rewrite found and still being verified in the background "
                    f"(over {rewrite_wait_sec:g}s) — not offered yet; check this query again to get it")
                break
            if res.passed:
                return GuardVerdict(
                    decision="rewrite",
                    reason=("verified rewrite available ("
                            + ("fewer bytes billed by dry run" if estimate is not None
                               and estimate.source == "dry_run"
                               else "same filter in a prunable form" if source.startswith("deterministic:")
                               and pruning_only else "not costlier by estimate")
                            + "): " + "; ".join(findings)),
                    rewritten_sql=cand,
                    findings=findings,
                    rewrite_source=source,
                    verification=res.detail + (" (remembered from an earlier check)"
                                               if how == "remembered" else ""),
                    **base,
                )
            findings.append(f"{source} rewrite failed verification — not offered ({res.detail})"
                            + (" (remembered)" if how == "remembered" else ""))

    if findings:
        return GuardVerdict(decision="warn", reason="; ".join(findings), findings=findings, **base)

    if estimate is None:
        reason = "clean by static rules — no backend, cost not estimated"
    elif estimate.source == "dry_run":  # BigQuery: bytes, no row count
        reason = f"clean — estimated {est_bytes or 0:,} bytes ({estimate.source}), within caps"
    else:
        reason = f"clean — estimated {rows:,} rows ({estimate.source}), within caps"
    return GuardVerdict(decision="allow", reason=reason, findings=[], **base)
