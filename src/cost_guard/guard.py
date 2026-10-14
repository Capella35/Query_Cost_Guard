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

from dataclasses import dataclass, field
from typing import Literal, Optional

from . import sql_ast

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
) -> GuardVerdict:
    """Guard pipeline: read-only check -> cost estimate -> static rules
    (per referenced table, with column profiles) -> hard caps for the
    workload class -> verdict -> (optional) LLM rewrite + explanation.

    customer: parsed customer.yaml; loaded from the repo root when None.
    """
    from .config import load_customer
    from .tracing import get_tracer

    customer = customer if customer is not None else load_customer()
    tracer = tracer or get_tracer()
    with tracer.trace("guard.decide", workload_class=workload_class) as tid:
        verdict = _decide(sql, workload_class, customer, backend, tracer, tid, llm)
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


def _decide(sql, workload_class, customer, backend, tracer, tid, llm) -> GuardVerdict:
    from .column_registry import registry_settings
    from .config import caps_for, partition_column
    from .rewrites import deterministic_rewrite
    from .rules.static_rules import ALL_RULES, LARGE_TABLE_ROWS, QUERY_RULES
    from .tools.compare_results import compare_results

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

    with tracer.span(tid, "estimate") as span:
        estimate = backend.estimate(sql) if backend is not None else None
        if estimate is not None:
            span["attrs"].update(source=estimate.source, rows=estimate.rows_processed)
    rows = estimate.rows_processed if estimate else 0
    base.update(
        estimated_rows=rows if estimate else None,
        estimated_bytes=estimate.estimated_bytes_scanned if estimate else None,
        estimate_source=estimate.source if estimate else None,
    )

    tables = sql_ast.referenced_tables(tree)
    group_cols = sql_ast.group_by_columns(tree)
    table_md: dict[str, dict] = {}
    no_md: dict[str, str] = {}
    no_profile: list[str] = []
    max_age_days = registry_settings(customer)["max_age_days"]
    registry = None
    if backend is not None and tables and (group_cols or sql_ast.summed_columns(tree)):
        with tracer.span(tid, "column_registry.lookup"):
            get = getattr(backend, "column_registry", None)
            try:
                registry = get() if get is not None else None
            except Exception:  # noqa: BLE001 — missing registry = visible skip
                registry = None
    for t in tables:
        if backend is None:
            no_md[t] = "no backend"
            continue
        try:
            md = dict(backend.table_metadata(t))
        except Exception as e:  # noqa: BLE001
            no_md[t] = f"metadata unavailable ({str(e).splitlines()[0][:60]})"
            continue
        md["table"] = t
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
        for col in group_cols:
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
        md["registry"] = registry
        md["registry_max_age_days"] = max_age_days
        table_md[t] = md
    table_rows = {t: md.get("num_rows") for t, md in table_md.items()}

    findings: list[str] = []
    skipped: list[str] = []

    def run(rule, md, label):
        with tracer.span(tid, f"rule.{rule.__name__}", table=md.get("table")):
            try:
                findings.extend(rule(sql, md))
            except NotImplementedError:
                skipped.append(f"{label}: not implemented")
            except Exception as e:  # noqa: BLE001 — a broken rule is visible, not fatal
                skipped.append(f"{label}: error ({type(e).__name__}: {str(e)[:80]})")

    for rule in ALL_RULES:
        if rule in QUERY_RULES:
            run(rule, {"table": None, "table_rows": table_rows}, rule.__name__)
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

    if any(f.lower().startswith("cartesian") for f in findings):
        return GuardVerdict(
            decision="block",
            reason="runaway join pattern: " + "; ".join(findings),
            findings=findings,
            **base,
        )

    if findings and backend is not None:
        # Deterministic fixers first (chained); then the LLM proposer if
        # configured. Either way an unverified rewrite is never offered.
        candidates = []
        with tracer.span(tid, "suggest_rewrite") as span:
            cand, applied = deterministic_rewrite(sql, findings)
            span["attrs"]["fixers"] = applied
        if cand:
            candidates.append(("deterministic:" + "+".join(applied), cand))
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
            if estimate is not None:
                cand_est = backend.estimate(cand)
                if cand_est.rows_processed > rows:
                    findings.append(f"{source} rewrite is not cheaper by estimate — not offered")
                    continue
            with tracer.span(tid, "compare_results", source=source):
                res = compare_results(sql, cand, backend=backend)
            if res.passed:
                return GuardVerdict(
                    decision="rewrite",
                    reason="verified rewrite available (not costlier by estimate): " + "; ".join(findings),
                    rewritten_sql=cand,
                    findings=findings,
                    rewrite_source=source,
                    verification=res.detail,
                    **base,
                )
            findings.append(f"{source} rewrite failed verification — not offered ({res.detail})")

    if findings:
        return GuardVerdict(decision="warn", reason="; ".join(findings), findings=findings, **base)

    if estimate is None:
        reason = "clean by static rules — no backend, cost not estimated"
    else:
        reason = f"clean — estimated {rows:,} rows ({estimate.source}), within caps"
    return GuardVerdict(decision="allow", reason=reason, findings=[], **base)
