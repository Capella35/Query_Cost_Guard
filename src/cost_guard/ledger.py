"""Workload report from the query log: does the Guard move behavior and money?

Every eval measures verdict correctness. This module reads what actually
happened (runs/query_log.jsonl, written by runtime.run_query) and answers:

1. Warn follow-through — after a warn/rewrite, what did that user run next
   (within followup_window_min)? adopted the offered rewrite / changed to a
   clean query / re-ran the same shape unchanged (ignored) / changed but
   still flagged / nothing. Needs the `user` field; runs without it are
   counted, not guessed.
2. Workload by shape — per fingerprint: runs, users, runtime, rows/bytes,
   modeled cost, share of the total. Ranks the repeated waste (the hourly
   dashboard, the query fifty analysts run) above one-off mistakes.
3. Result-cache opportunity — completed runs whose exact query (keyed
   sql_hash) already completed within cache_ttl_min: work a result cache
   would have skipped, ASSUMING the data did not change in between (the log
   cannot see table modification times).
4. Spend ledger — executed vs killed vs blocked, plus bypasses and the
   false-block proxy (a blocked shape later bypassed). Blocked work is a
   counterfactual upper bound: the estimate of what would have run, not
   money saved — the analyst may have run a fixed version instead (that run
   is in the log and counted as executed).
5. Block follow-through — after a block, what did that user run next
   (same window)? fixed it (a different shape that ran), retried it unchanged
   (same shape, blocked again), changed it but still blocked, bypassed it,
   or nothing. Checks (check_query, `cost_guard check`) are not logged, so a
   revision checked but never run is invisible here; the next RUN is counted.
6. Guard health — decision mix and rates, Guard latency (p50/p95 of the
   check, from check_ms), and estimate accuracy where the warehouse reports
   billed bytes (BigQuery: billed / estimated, per executed run).
7. LLM usage — from the trace file, when one is given: calls, tokens and
   errors per LLM span (llm.*), so the "capped LLM" rule is checkable.

Dollars are MODELED (customer.yaml cost_model): bytes x price_per_tb_usd
where bytes exist (BigQuery), else runtime x price_per_runtime_hour_usd when
set (time-billed warehouses); otherwise the report stays in seconds and rows.
A before/after number needs real billing data (INFORMATION_SCHEMA.JOBS) from
a pilot; this report is the in-product proxy.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

FLAGGED = ("warn", "rewrite")
TB = 1024**4


def _ts(r: dict) -> datetime:
    return datetime.fromisoformat(r["ts"])


def _settings(customer: Optional[dict]) -> dict:
    cm = (customer or {}).get("cost_model") or {}
    rep = (customer or {}).get("report") or {}
    return {
        "price_per_tb": cm.get("price_per_tb_usd"),
        "price_per_hour": cm.get("price_per_runtime_hour_usd"),
        "followup_window": timedelta(minutes=rep.get("followup_window_min", 30)),
        "cache_ttl": timedelta(minutes=rep.get("cache_ttl_min", 60)),
    }


def modeled_cost(r: dict, s: dict) -> Optional[float]:
    """MODELED dollars for one executed run, or None when nothing prices it."""
    if r.get("bytes_estimate") and s["price_per_tb"]:
        return r["bytes_estimate"] / TB * s["price_per_tb"]
    if r.get("duration_sec") is not None and s["price_per_hour"]:
        return r["duration_sec"] / 3600 * s["price_per_hour"]
    return None


def _executed(r: dict) -> bool:
    return r["status"] in ("completed", "killed", "error")


def warn_followthrough(log: list[dict], s: dict) -> dict:
    by_user: dict[str, list[dict]] = defaultdict(list)
    no_user = 0
    for r in sorted(log, key=_ts):
        if r.get("user"):
            by_user[r["user"]].append(r)
        elif r["decision"] in FLAGGED:
            no_user += 1
    counts = dict.fromkeys(("adopted_rewrite", "changed_to_clean", "reran_unchanged",
                            "changed_still_flagged", "no_followup"), 0)
    for runs in by_user.values():
        for i, r in enumerate(runs):
            if r["decision"] not in FLAGGED:
                continue
            nxt = runs[i + 1] if i + 1 < len(runs) else None
            if nxt is None or _ts(nxt) - _ts(r) > s["followup_window"]:
                counts["no_followup"] += 1
            elif r.get("rewrite_fingerprint") and nxt["fingerprint"] == r["rewrite_fingerprint"]:
                counts["adopted_rewrite"] += 1
            elif nxt["fingerprint"] == r["fingerprint"]:
                counts["reran_unchanged"] += 1
            elif nxt["decision"] == "allow":
                counts["changed_to_clean"] += 1
            else:
                counts["changed_still_flagged"] += 1
    n = sum(counts.values())
    acted = counts["adopted_rewrite"] + counts["changed_to_clean"]
    return {
        "flagged_runs_with_user": n,
        "flagged_runs_without_user": no_user,
        **counts,
        # Upper bound: "changed_to_clean" may be an unrelated next query.
        "acted_rate_upper_bound": round(acted / n, 3) if n else None,
        "ignored_rate": round(counts["reran_unchanged"] / n, 3) if n else None,
    }


def block_followthrough(log: list[dict], s: dict) -> dict:
    by_user: dict[str, list[dict]] = defaultdict(list)
    no_user = 0
    for r in sorted(log, key=_ts):
        if r.get("user"):
            by_user[r["user"]].append(r)
        elif r["status"] == "blocked":
            no_user += 1
    counts = dict.fromkeys(("fixed_and_ran", "retried_unchanged", "changed_still_blocked",
                            "bypassed", "no_followup"), 0)
    for runs in by_user.values():
        for i, r in enumerate(runs):
            if r["status"] != "blocked":
                continue
            nxt = runs[i + 1] if i + 1 < len(runs) else None
            if nxt is None or _ts(nxt) - _ts(r) > s["followup_window"]:
                counts["no_followup"] += 1
            elif nxt.get("bypassed") and nxt["fingerprint"] == r["fingerprint"]:
                counts["bypassed"] += 1
            elif nxt["fingerprint"] == r["fingerprint"]:
                counts["retried_unchanged"] += 1
            elif nxt["status"] == "blocked":
                counts["changed_still_blocked"] += 1
            else:
                counts["fixed_and_ran"] += 1
    n = sum(counts.values())
    return {
        "blocked_runs_with_user": n,
        "blocked_runs_without_user": no_user,
        **counts,
        # Upper bound: "fixed_and_ran" may be an unrelated next query.
        "fixed_rate_upper_bound": round(counts["fixed_and_ran"] / n, 3) if n else None,
        "retry_rate": round(counts["retried_unchanged"] / n, 3) if n else None,
    }


def workload_by_shape(log: list[dict], s: dict, top: int = 10) -> list[dict]:
    shapes: dict[str, dict] = {}
    for r in log:
        sh = shapes.setdefault(r["fingerprint"], {
            "fingerprint": r["fingerprint"], "runs": 0, "executed": 0, "users": set(),
            "runtime_sec": 0.0, "rows": 0, "cost_usd": 0.0, "priced": False,
            "decisions": defaultdict(int)})
        sh["runs"] += 1
        sh["decisions"][r["decision"]] += 1
        if r.get("user"):
            sh["users"].add(r["user"])
        if _executed(r):
            sh["executed"] += 1
            sh["runtime_sec"] += r.get("duration_sec") or 0.0
            sh["rows"] += r.get("rows_estimate") or 0
            c = modeled_cost(r, s)
            if c is not None:
                sh["cost_usd"] += c
                sh["priced"] = True
    total_rt = sum(sh["runtime_sec"] for sh in shapes.values()) or 1.0
    out = []
    for sh in shapes.values():
        out.append({
            "fingerprint": sh["fingerprint"],
            "runs": sh["runs"],
            "executed": sh["executed"],
            "users": len(sh["users"]),
            "runtime_sec": round(sh["runtime_sec"], 3),
            "runtime_share": round(sh["runtime_sec"] / total_rt, 3),
            "rows_estimate_total": sh["rows"],
            "modeled_cost_usd": round(sh["cost_usd"], 4) if sh["priced"] else None,
            "decisions": dict(sh["decisions"]),
        })
    key = (lambda x: (x["modeled_cost_usd"] or 0, x["runtime_sec"]))
    return sorted(out, key=key, reverse=True)[:top]


def cache_opportunity(log: list[dict], s: dict) -> dict:
    last_done: dict[str, datetime] = {}
    hits = []
    for r in sorted(log, key=_ts):
        h = r.get("sql_hash")
        if not h or r["status"] != "completed":
            continue
        prev = last_done.get(h)
        if prev is not None and _ts(r) - prev <= s["cache_ttl"]:
            hits.append(r)
        last_done[h] = _ts(r)
    completed = [r for r in log if r["status"] == "completed" and r.get("sql_hash")]
    costs = [modeled_cost(r, s) for r in hits]
    return {
        "completed_runs_with_hash": len(completed),
        "repeat_within_ttl": len(hits),
        "repeat_rate": round(len(hits) / len(completed), 3) if completed else None,
        "runtime_sec_recomputed": round(sum(r.get("duration_sec") or 0 for r in hits), 3),
        "modeled_cost_usd_recomputed": (round(sum(c for c in costs if c is not None), 4)
                                        if any(c is not None for c in costs) else None),
        "assumption": "data unchanged between repeats (table modification times not logged)",
    }


def spend_ledger(log: list[dict], s: dict) -> dict:
    def total(rs, field):
        return sum(r.get(field) or 0 for r in rs)

    def cost(rs):
        cs = [modeled_cost(r, s) for r in rs]
        return round(sum(c for c in cs if c is not None), 4) if any(c is not None for c in cs) else None

    executed = [r for r in log if _executed(r)]
    killed = [r for r in log if r["status"] == "killed"]
    blocked = [r for r in log if r["status"] == "blocked"]
    bypassed = [r for r in log if r.get("bypassed")]
    blocked_shapes = {r["fingerprint"] for r in blocked}
    # Proxy for a false block: someone needed the shape enough to bypass it.
    false_block_proxy = {r["fingerprint"] for r in bypassed} & blocked_shapes
    weeks: dict[str, dict] = defaultdict(lambda: {"runs": 0, "runtime_sec": 0.0, "blocked": 0})
    for r in log:
        wk = _ts(r).strftime("%G-W%V")
        weeks[wk]["runs"] += 1
        if _executed(r):
            weeks[wk]["runtime_sec"] = round(weeks[wk]["runtime_sec"] + (r.get("duration_sec") or 0), 3)
        if r["status"] == "blocked":
            weeks[wk]["blocked"] += 1
    return {
        "runs": len(log),
        "executed": {"runs": len(executed), "runtime_sec": round(total(executed, "duration_sec"), 3),
                     "rows_estimate": total(executed, "rows_estimate"), "modeled_cost_usd": cost(executed)},
        "killed": {"runs": len(killed), "runtime_sec_before_kill": round(total(killed, "duration_sec"), 3)},
        "blocked": {"runs": len(blocked), "rows_estimate_not_run": total(blocked, "rows_estimate"),
                    "bytes_estimate_not_run": total(blocked, "bytes_estimate"),
                    "note": "counterfactual upper bound, not money saved"},
        "bypassed": len(bypassed),
        "false_block_proxy_shapes": len(false_block_proxy),
        "by_week": dict(sorted(weeks.items())),
    }


def _pct(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    import math

    v = sorted(values)  # nearest rank
    return round(v[max(0, math.ceil(q * len(v)) - 1)], 1)


def guard_health(log: list[dict]) -> dict:
    n = len(log)
    decisions: dict[str, int] = defaultdict(int)
    for r in log:
        decisions[r["decision"]] += 1
    latency = [r["check_ms"] for r in log if r.get("check_ms") is not None]
    ratios = sorted(r["bytes_billed"] / r["bytes_estimate"] for r in log
                    if _executed(r) and r.get("bytes_billed") is not None and r.get("bytes_estimate"))

    def rate(k):
        return round(decisions.get(k, 0) / n, 4) if n else None

    return {
        "decisions": dict(sorted(decisions.items())),
        "block_rate": rate("block"),
        "flag_rate": round(sum(decisions.get(k, 0) for k in ("warn", "rewrite", "block")) / n, 4) if n else None,
        "bypass_rate": round(sum(1 for r in log if r.get("bypassed")) / n, 4) if n else None,
        "check_ms": {"runs": len(latency), "p50": _pct(latency, 0.5), "p95": _pct(latency, 0.95)},
        # Completed runs that broke their shape's own history (anomaly.py); each
        # may have a report in runs/investigations/.
        "anomalies": {
            "runs": sum(1 for r in log if r.get("anomaly")),
            "size": sum(1 for r in log if (r.get("anomaly") or "").startswith("adaptive anomaly: rows")),
            "runtime": sum(1 for r in log if (r.get("anomaly") or "").startswith("adaptive anomaly: runtime")),
            "shapes_flagged": len({r["fingerprint"] for r in log if r.get("anomaly")}),
        },
        "estimate_accuracy": {
            "runs": len(ratios),
            "median_billed_over_estimate": round(ratios[len(ratios) // 2], 3) if ratios else None,
            "note": "billed bytes from the warehouse (BigQuery only); 1.0 = estimate exact",
        },
    }


def llm_usage(traces: list[dict]) -> dict:
    """LLM calls and tokens from trace spans named llm.* (counts only)."""
    by_name: dict[str, dict] = defaultdict(lambda: {"calls": 0, "errors": 0, "input_tokens": 0,
                                                    "output_tokens": 0})
    for sp in traces:
        if not sp.get("name", "").startswith("llm."):
            continue
        a, row = sp.get("attrs") or {}, by_name[sp["name"]]
        row["calls"] += 1
        row["errors"] += 1 if a.get("error") else 0
        row["input_tokens"] += a.get("input_tokens") or 0
        row["output_tokens"] += a.get("output_tokens") or 0
    return {
        "by_span": dict(sorted(by_name.items())),
        "calls": sum(r["calls"] for r in by_name.values()),
        "input_tokens": sum(r["input_tokens"] for r in by_name.values()),
        "output_tokens": sum(r["output_tokens"] for r in by_name.values()),
    }


def build_report(log: list[dict], customer: Optional[dict] = None, top: int = 10,
                 traces: Optional[list[dict]] = None) -> dict:
    """traces: spans from the trace file (tracing.read_traces); adds LLM usage."""
    s = _settings(customer)
    rep = {
        "cost_unit": ("MODELED USD from bytes" if s["price_per_tb"] and any(r.get("bytes_estimate") for r in log)
                      else "MODELED USD from runtime" if s["price_per_hour"]
                      else "runtime seconds and estimated rows (no price set)"),
        "warn_followthrough": warn_followthrough(log, s),
        "block_followthrough": block_followthrough(log, s),
        "workload_by_shape": workload_by_shape(log, s, top),
        "cache_opportunity": cache_opportunity(log, s),
        "spend_ledger": spend_ledger(log, s),
        "guard_health": guard_health(log),
    }
    if traces is not None:
        rep["llm_usage"] = llm_usage(traces)
    return rep
