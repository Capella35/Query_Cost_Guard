"""Spend budgets per user, per team and per workload class (owner's choice
2026-10-18), decided before a query runs like every other check.

    spent      = the scope's runs this period, priced          (query log)
    this_query = max(dry-run bytes, 10 MiB x tables) x price  (free dry run)
    projected  = spent + this_query

projected >= warn_at x limit warns; projected > limit blocks. A query counts
toward its user's budget and every team the user belongs to; the stricter
(the highest projected share) decides. A workload class with its own budget
(customer.yaml `budgets.classes`, e.g. scheduled jobs) counts toward that
budget only, not toward the user's or a team's.

Period: the calendar month, UTC (resets on the 1st). A run is priced by what
it billed (`bytes_billed`, BigQuery) or, without that, by ledger.modeled_cost;
a blocked run spends nothing. Every dollar figure is MODELED.

Identity: the user must come from the deployment (the CLI's person at the
terminal, the MCP server's --user), never from a caller who could pass
someone else's id. Without one, user and team budgets are skipped visibly.

Known limit: two queries checked at the same moment see the same `spent`, so
the limit can be overshot by about one query — bounded by the per-query caps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

TB = 1024**4
DEFAULTS = {
    "enabled": False,
    "warn_at": 0.8,      # share of a limit at which the Guard warns
    "user_usd": None,    # every user's monthly limit (None = no user budgets)
    "users": {},         # per-user overrides: {user: usd}
    "teams": {},         # {team: {"usd": limit, "members": [user, ...]}}
    "classes": {},       # {workload class: usd}: that class's runs count only here
}


def settings(customer: Optional[dict]) -> dict:
    return {**DEFAULTS, **((customer or {}).get("budgets") or {})}


def period_start(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass
class Scope:
    kind: str  # "user" | "team" | "class"
    name: str
    limit_usd: float
    spent_usd: float


@dataclass
class BudgetState:
    """The scopes a query from `user` in `workload_class` counts toward."""
    period_start: str
    scopes: list[Scope] = field(default_factory=list)
    skipped: Optional[str] = None  # why user/team budgets don't apply


def run_cost_usd(r: dict, customer: Optional[dict]) -> float:
    """MODELED dollars one logged run spent: what it billed, else the
    ledger's model; 0 for a run that did not execute."""
    if r.get("status") not in ("completed", "killed", "error"):
        return 0.0
    price = ((customer or {}).get("cost_model") or {}).get("price_per_tb_usd") or 0
    if r.get("bytes_billed") is not None:
        return r["bytes_billed"] / TB * price
    from .ledger import _settings, modeled_cost

    return modeled_cost(r, _settings(customer)) or 0.0


def _in_period(r: dict, start: datetime) -> bool:
    try:
        return datetime.fromisoformat(r["ts"]) >= start
    except (KeyError, ValueError):
        return False


def budget_state(log: list[dict], customer: Optional[dict], user: Optional[str],
                 workload_class: str, now: Optional[datetime] = None) -> Optional[BudgetState]:
    """None when budgets are off; otherwise the scopes this query counts
    toward, with what each has spent this period."""
    cfg = settings(customer)
    if not cfg["enabled"]:
        return None
    start = period_start(now)
    runs = [r for r in log if _in_period(r, start)]
    classes = {k: float(v) for k, v in (cfg["classes"] or {}).items()}
    state = BudgetState(period_start=start.isoformat())

    def spent(pred) -> float:
        return round(sum(run_cost_usd(r, customer) for r in runs if pred(r)), 6)

    if workload_class in classes:
        state.scopes.append(Scope("class", workload_class, classes[workload_class],
                                  spent(lambda r: r.get("workload_class") == workload_class)))
        return state
    general = lambda r: r.get("workload_class") not in classes  # noqa: E731
    if not user:
        state.skipped = "no trusted user identity (the deployment sets it): user and team budgets skipped"
        return state
    limit = (cfg["users"] or {}).get(user, cfg["user_usd"])
    if limit is not None:
        state.scopes.append(Scope("user", user, float(limit),
                                  spent(lambda r: general(r) and r.get("user") == user)))
    for team, t in (cfg["teams"] or {}).items():
        members = set(t.get("members") or [])
        if user in members and t.get("usd") is not None:
            state.scopes.append(Scope("team", team, float(t["usd"]),
                                      spent(lambda r, m=members: general(r) and r.get("user") in m)))
    return state


def query_cost_usd(estimated_bytes: Optional[int], tables: int, customer: Optional[dict]) -> float:
    """MODELED dollars this query would bill, from its dry-run bytes (BigQuery
    bills at least 10 MiB per table). 0 without a bytes estimate."""
    if not estimated_bytes:
        return 0.0
    from .runtime import bigquery_billed

    price = ((customer or {}).get("cost_model") or {}).get("price_per_tb_usd") or 0
    return bigquery_billed(estimated_bytes, tables) / TB * price


def judge(state: BudgetState, this_usd: float, warn_at: float):
    """(level, scope, projected) for the stricter scope: level "block" (over
    the limit), "warn" (at or past warn_at), or None."""
    worst, worst_share = None, -1.0
    for s in state.scopes:
        share = (s.spent_usd + this_usd) / s.limit_usd if s.limit_usd > 0 else float("inf")
        if share > worst_share:
            worst, worst_share = s, share
    if worst is None:
        return None, None, None
    projected = worst.spent_usd + this_usd
    if projected > worst.limit_usd:
        return "block", worst, projected
    if projected >= warn_at * worst.limit_usd:
        return "warn", worst, projected
    return None, worst, projected


def status(log: list[dict], customer: Optional[dict], now: Optional[datetime] = None) -> Optional[dict]:
    """Every configured budget's spend this period (for reports). Users: the
    ones with an override plus everyone who ran something this period."""
    cfg = settings(customer)
    if not cfg["enabled"]:
        return None
    start = period_start(now)
    rows = []
    users = set(cfg["users"] or {}) | {r["user"] for r in log
                                       if r.get("user") and _in_period(r, start)}
    seen = set()
    for u in sorted(users):
        st = budget_state(log, customer, u, "interactive", now)
        for s in st.scopes:
            if (s.kind, s.name) not in seen:
                seen.add((s.kind, s.name))
                rows.append(s)
    for c in cfg["classes"] or {}:
        rows += budget_state(log, customer, None, c, now).scopes
    return {"period_start": start.isoformat(), "warn_at": cfg["warn_at"], "budgets": [
        {"kind": s.kind, "name": s.name, "limit_usd": s.limit_usd, "spent_usd_modeled": s.spent_usd,
         "share": round(s.spent_usd / s.limit_usd, 4) if s.limit_usd else None} for s in rows]}
