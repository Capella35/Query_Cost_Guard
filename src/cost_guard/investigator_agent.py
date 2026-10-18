"""Report-only investigator agent: started when a run breaks its own history.

Trigger: the adaptive anomaly rule (anomaly.py) flags a completed run in
runtime.run_query; the MCP server starts an investigation in the background,
and `python -m cost_guard investigate-anomalies` runs any the log holds that
have no report yet (offline / batch).

It changes NOTHING: no rules, caps, thresholds, kills, rewrites or labels.
Its output is a report (runs/investigations/<id>.json and .md) and one
incident in episodic memory, so the next investigation of the shape can say
"this has happened before". Every report says `changes_made: []`. (The older
investigator.py proposes thresholds through gates and a shadow trial; this
one does not touch that path.)

Fixed steps, each a call to the MCP server's tools (CostGuardService) and
traced as agent.step.* under one agent.investigate trace:
  1. history     get_query_history: runs, statuses, median, recent runs
  2. tables      get_table_info for the tables the shape reads
  3. check       check_query on the triggering SQL — only when it is in hand
                 (live trigger; in memory, never written). Batch runs skip it.
  4. neighbours  other shapes that ran just before: did they slow down too?
                 (warehouse-wide load vs this shape)
  5. memory      past incidents of this shape
  6. diagnose    deterministic rules over the facts -> likely causes, ranked,
                 and what to look at next
  7. summary     OPTIONAL: one Claude call for a plain-language note, from
                 the facts above only (no SQL). Off unless customer.yaml
                 investigator.llm_max_per_day > 0; capped per day; cached by
                 the facts, so the same facts never cost a second call.

Limits (customer.yaml investigator): a cooldown per shape and a daily cap on
investigations, so a shape that keeps breaking its history cannot flood the
reports (or the LLM budget).

Reports hold shape ids, numbers, rule names, table names and the
deterministic text below — never SQL text, literals or result rows, and not
rule findings (some quote the query). The fingerprint stays in the JSON (it
is literal-free and stays in-customer, like the query log).
"""
from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .anomaly import accepted_until
from .memory import EpisodicMemory, Incident
from .tracing import Tracer, annotate

DEFAULT_DIR = Path("runs/investigations")
NEIGHBOUR_WINDOW_MIN = 30  # "ran just before": other shapes' runs in this window
SLOW_RATIO = 1.5  # a neighbour run this far above its own shape's median counts as slow


def settings(customer: Optional[dict]) -> dict:
    inv = (customer or {}).get("investigator") or {}
    return {
        "enabled": inv.get("enabled", True),
        "cooldown_min": inv.get("cooldown_min", 60),
        "max_per_day": inv.get("max_per_day", 20),
        "llm_max_per_day": inv.get("llm_max_per_day", 0),
    }


@dataclass
class InvestigationReport:
    id: str
    ts: str
    trigger_ts: str
    fingerprint: str
    shape_id: str
    anomaly_kind: str  # "size" | "runtime"
    trigger: dict  # the flagged run's numbers + the rule's reason
    facts: dict = field(default_factory=dict)
    likely_causes: list[str] = field(default_factory=list)
    look_next: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    summary: Optional[str] = None
    summary_source: str = "none"  # "none" | "llm" | "llm-cache"
    changes_made: list = field(default_factory=list)  # always empty: report-only
    trace_id: Optional[str] = None


class InvestigatorAgent:
    def __init__(self, service, customer: Optional[dict] = None, out_dir: str | Path = DEFAULT_DIR,
                 memory: Optional[EpisodicMemory] = None, llm=None):
        """service: a CostGuardService (the MCP tools). llm: anything with
        summarize_investigation(facts) — used only within llm_max_per_day."""
        self.service = service
        self.customer = customer if customer is not None else service.customer
        self.out_dir = Path(out_dir)
        self.memory = memory or EpisodicMemory(self.out_dir / "incidents.jsonl")
        self.llm = llm
        self.cfg = settings(self.customer)

    # ---- limits ---------------------------------------------------------

    def _reports(self) -> list[dict]:
        if not self.out_dir.exists():
            return []
        return [json.loads(p.read_text()) for p in sorted(self.out_dir.glob("*.json"))
                if p.name != "summary_cache.json"]

    def should_investigate(self, fingerprint: str, trigger_ts: str) -> tuple[bool, str]:
        if not self.cfg["enabled"]:
            return False, "investigator disabled (customer.yaml investigator.enabled)"
        reports = self._reports()
        if any(r["fingerprint"] == fingerprint and r["trigger_ts"] == trigger_ts for r in reports):
            return False, "already investigated"
        now = datetime.fromisoformat(trigger_ts)
        day = [r for r in reports if now - datetime.fromisoformat(r["trigger_ts"]) < timedelta(days=1)]
        if len(day) >= self.cfg["max_per_day"]:
            return False, f"daily cap reached ({self.cfg['max_per_day']} investigations)"
        cool = timedelta(minutes=self.cfg["cooldown_min"])
        if any(r["fingerprint"] == fingerprint
               and timedelta(0) <= now - datetime.fromisoformat(r["trigger_ts"]) < cool for r in day):
            return False, f"cooldown: this shape was investigated in the last {self.cfg['cooldown_min']} min"
        return True, "ok"

    # ---- the investigation ----------------------------------------------

    def investigate(self, event: dict) -> Optional[InvestigationReport]:
        """event: runtime.run_query's anomaly event (or one rebuilt from the
        log; then without "sql"). Returns None when a limit skips it."""
        from .mcp_server import shape_id

        fp, trigger_ts = event["fingerprint"], event["ts"]
        ok, why = self.should_investigate(fp, trigger_ts)
        if not ok:
            return None
        tracer = Tracer()
        now = datetime.now(timezone.utc)
        rep = InvestigationReport(
            id=f"{now:%Y%m%dT%H%M%S}_{shape_id(fp)}", ts=now.isoformat(), trigger_ts=trigger_ts,
            fingerprint=fp, shape_id=shape_id(fp), anomaly_kind=event["kind"],
            trigger={k: event.get(k) for k in ("reason", "duration_sec", "size", "history_runs",
                                                 "history_median_sec", "workload_class")})
        try:
            with tracer.trace("agent.investigate", shape_id=rep.shape_id, kind=rep.anomaly_kind) as tid:
                rep.trace_id = tid
                self._run_steps(rep, event, tracer, tid)
        finally:
            if self.service.trace_path is not None:
                tracer.save(self.service.trace_path)
        self._write(rep)
        self.memory.record(Incident(fp, rep.likely_causes[0] if rep.likely_causes else "no clear cause",
                                    outcome="reported (no change)", ts=trigger_ts))
        return rep

    def _step(self, rep, tracer, tid, name: str, fn):
        with tracer.span(tid, f"agent.step.{name}") as span:
            try:
                out = fn()
                rep.steps.append(f"{name}: ok")
                return out
            except Exception as e:  # noqa: BLE001 — a failed step is reported, not fatal
                span["attrs"]["error"] = type(e).__name__
                rep.steps.append(f"{name}: failed ({type(e).__name__})")
                return None

    def _run_steps(self, rep: InvestigationReport, event: dict, tracer, tid) -> None:
        from . import sql_ast

        fp = rep.fingerprint
        hist = self._step(rep, tracer, tid, "history", lambda: self.service.get_query_history(fp))
        # This runs in the agent's own thread, where the dialect context of the
        # request is not set: parse in the backend's dialect (a BigQuery
        # fingerprint with backticked tables failed under DuckDB's, 2026-10-18).
        dialect = "bigquery" if getattr(self.service.backend, "name", None) == "bigquery" else sql_ast.DIALECT
        tree = sql_ast.try_parse(fp, dialect)
        tables = sql_ast.referenced_tables(tree) if tree is not None else []
        tinfo = self._step(rep, tracer, tid, "tables", lambda: {
            t: self.service.get_table_info(t) for t in tables})
        check = None
        if event.get("sql"):
            check = self._step(rep, tracer, tid, "check", lambda: self.service.check_query(
                event["sql"], event.get("workload_class")))
        else:
            rep.steps.append("check: skipped (no SQL in hand: batch run from the log)")
        neigh = self._step(rep, tracer, tid, "neighbours", lambda: self._neighbours(fp, rep.trigger_ts))
        past = self._step(rep, tracer, tid, "memory", lambda: [
            asdict(i) for i in self.memory.recall(fp, limit=10) if i.ts < rep.trigger_ts])

        rep.facts = {
            "history": _history_facts(hist, rep.trigger, rep.trigger_ts,
                                      accepted_until(self.service.log_path, fp)),
            "tables": {t: {k: v.get(k) for k in ("num_rows", "num_bytes", "partitioned_by")}
                       for t, v in (tinfo or {}).items()},
            "check": ({k: check.get(k) for k in ("decision", "estimated_rows", "estimated_bytes",
                                                   "estimate_source", "rules_fired")}
                      if check else None),
            "neighbours": neigh,
            "past_incidents": len(past or []),
            "shape_id": rep.shape_id,
        }
        with tracer.span(tid, "agent.step.diagnose"):
            rep.likely_causes, rep.look_next = diagnose(rep.anomaly_kind, rep.trigger, rep.facts)
            annotate(causes=len(rep.likely_causes))
        rep.steps.append("diagnose: ok")
        if self.llm is not None and self.cfg["llm_max_per_day"] > 0:
            with tracer.span(tid, "llm.summarize_investigation"):
                self._summarize(rep)

    def _neighbours(self, fp: str, trigger_ts: str) -> dict:
        """Other shapes' completed runs in the window before the trigger, each
        against its own shape's median: a slowdown everywhere points at the
        warehouse, not at this query."""
        from .runtime import read_log

        log = read_log(self.service.log_path)
        t = datetime.fromisoformat(trigger_ts)
        lo = (t - timedelta(minutes=NEIGHBOUR_WINDOW_MIN)).isoformat()
        medians: dict[str, float] = {}
        by_fp: dict[str, list[float]] = {}
        for r in log:
            if r["status"] == "completed" and r.get("duration_sec") and r["ts"] < lo:
                by_fp.setdefault(r["fingerprint"], []).append(r["duration_sec"])
        for k, ds in by_fp.items():
            if len(ds) >= 5:
                medians[k] = statistics.median(ds)
        recent = [r for r in log if r["fingerprint"] != fp and r["status"] == "completed"
                  and r.get("duration_sec") and lo <= r["ts"] <= trigger_ts
                  and r["fingerprint"] in medians]
        slow = [r for r in recent if r["duration_sec"] > SLOW_RATIO * medians[r["fingerprint"]]]
        return {"window_min": NEIGHBOUR_WINDOW_MIN, "runs": len(recent),
                "shapes": len({r["fingerprint"] for r in recent}), "slow_runs": len(slow),
                "slow_shapes": len({r["fingerprint"] for r in slow})}

    def _summarize(self, rep: InvestigationReport) -> None:
        facts = {"anomaly_kind": rep.anomaly_kind, "trigger": rep.trigger, "facts": rep.facts,
                 "likely_causes": rep.likely_causes, "look_next": rep.look_next}
        key = hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()[:24]
        cache_path = self.out_dir / "summary_cache.json"
        try:
            cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        except ValueError:
            cache = {}
        if key in cache:
            rep.summary, rep.summary_source = cache[key], "llm-cache"
            return
        # The cap counts calls in their own append-only log, not reports: deleting
        # reports must not reset the day's budget.
        usage = self.out_dir / "llm_usage.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        used = (sum(1 for line in usage.read_text().splitlines()
                    if line.strip() and json.loads(line)["ts"][:10] == today)
                if usage.exists() else 0)
        if used >= self.cfg["llm_max_per_day"]:
            rep.steps.append(f"summary: skipped (LLM cap {self.cfg['llm_max_per_day']}/day reached)")
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with usage.open("a") as f:  # counted before the call: a failed call still spent
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "report": rep.id}) + "\n")
        try:
            text = self.llm.summarize_investigation(facts)
        except Exception as e:  # noqa: BLE001 — the deterministic report stands
            rep.steps.append(f"summary: failed ({type(e).__name__})")
            return
        if text:
            rep.summary, rep.summary_source = text, "llm"
            cache[key] = text
            self.out_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=1))
            rep.steps.append("summary: ok (llm)")

    def _write(self, rep: InvestigationReport) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / f"{rep.id}.json").write_text(json.dumps(asdict(rep), indent=2, default=str))
        (self.out_dir / f"{rep.id}.md").write_text(render_markdown(rep))


def _history_facts(hist: Optional[dict], trigger: dict, trigger_ts: str,
                   accepted: Optional[str] = None) -> Optional[dict]:
    """The shape's runs BEFORE the trigger, in the anomaly rule's own size
    (anomaly_size: two-way rows on DuckDB, bytes on BigQuery)."""
    if not hist:
        return None
    before = [r for r in hist["recent"] if r["ts"] < trigger_ts]
    done = [r for r in before if r["status"] == "completed"]
    sizes = [r["anomaly_size"] for r in done if r.get("anomaly_size") is not None]
    durs = [r["duration_sec"] for r in done if r.get("duration_sec") is not None]
    med_size = statistics.median(sizes) if sizes else None
    med_dur = statistics.median(durs) if durs else None
    ratio = lambda x, m: round(x / m, 1) if x is not None and m else None  # noqa: E731
    return {"runs_before": len(before), "users": hist["users"], "statuses": hist["statuses"],
            "median_duration_sec": med_dur, "median_size": med_size,
            "this_run_size_x_median": ratio(trigger.get("size"), med_size),
            "this_run_duration_x_median": ratio(trigger.get("duration_sec"), med_dur),
            "killed_last_20": sum(1 for r in before[-20:] if r["status"] == "killed"),
            "earlier_anomalies": sum(1 for r in before if r.get("anomaly")),
            # Since the last acceptance only: accepted flags are the shape's normal now.
            "anomalies_in_last_10": sum(1 for r in before[-10:] if r.get("anomaly")
                                        and (accepted is None or r["ts"] > accepted)),
            "accepted_normal_at": accepted}


def diagnose(kind: str, trigger: dict, facts: dict) -> tuple[list[str], list[str]]:
    """Deterministic: likely causes (ranked) and what to look at next."""
    causes, nxt = [], []
    neigh = facts.get("neighbours") or {}
    check = facts.get("check") or {}
    rules = set(check.get("rules_fired") or [])
    hist = facts.get("history") or {}
    wide = neigh.get("slow_shapes", 0) >= 2 and neigh.get("slow_runs", 0) * 2 >= max(neigh.get("runs", 0), 1)

    if kind == "size":
        x = hist.get("this_run_size_x_median")
        causes.append((f"this run reads {x}x its shape's median size" if x else
                       "this run reads far more than its shape usually does")
                      + ": the same query shape "
                      "with different constants (e.g. a wider date range or a broader filter value)"
                      " — or the tables it reads have grown")
        if "require_partition_filter" in rules:
            causes.append("no filter on the partition/date column: the input grows with the table, "
                          "so every run reads more as data arrives")
        nxt.append("compare this run's filter values with the usual ones (the caller knows them; "
                   "the log keeps only the shape)")
        nxt.append("check the tables' sizes over time (facts.tables shows today's)")
    else:
        if wide:
            causes.append(f"warehouse-wide slowdown: {neigh['slow_shapes']} other query shapes ran "
                          f">{SLOW_RATIO}x their own median in the {neigh['window_min']} min before "
                          "— load or contention, not this query")
            nxt.append("look at warehouse load / concurrent jobs at that time")
        else:
            causes.append("this shape alone ran slower while reading a usual amount of data: a plan "
                          "change, data skew, or contention on its tables"
                          + (" (other shapes ran normally in the same window)"
                             if neigh.get("runs") else " (no other shapes ran in the window to compare)"))
            nxt.append("compare this run's plan with an earlier run of the shape (EXPLAIN)")
        if hist.get("killed_last_20"):
            causes.append(f"{hist['killed_last_20']} of the shape's last 20 runs were killed at "
                          "the runtime limit — the slowdown may be ongoing")
    if hist.get("anomalies_in_last_10", 0) >= 2:
        # With flagged runs left out of the baseline (customer.yaml
        # anomaly.exclude_flagged), a new normal keeps being flagged.
        causes.insert(0, f"looks like a lasting change, not a one-off: {hist['anomalies_in_last_10'] + 1} "
                         "of this shape's last 11 runs were flagged — if this is the new normal, a "
                         "person should accept it (flagged runs stay out of the baseline until then)")
        nxt.insert(0, f"if this is the new normal: python -m cost_guard accept-normal "
                      f"{facts.get('shape_id') or '<shape_id>'} --by <you> --note <why>")
    if facts.get("past_incidents"):
        causes.append(f"recurring: {facts['past_incidents']} earlier incident(s) for this shape")
        nxt.append("a recurring shape may need a structural fix (a summary table, partitioning, "
                   "a narrower query) — a person decides")
    if check.get("decision") in ("warn", "rewrite") and rules:
        nxt.append("the Guard flags this shape today (" + ", ".join(sorted(rules)) + "): see "
                   "check_query's findings for the query")
    return causes, nxt


def render_markdown(rep: InvestigationReport) -> str:
    t = rep.trigger
    lines = [f"# Investigation {rep.id}", "",
             f"- Query shape: `{rep.shape_id}` · anomaly: **{rep.anomaly_kind}** · run at {rep.trigger_ts}",
             f"- Trigger: {t.get('reason')}",
             f"- Changes made: none (report-only)", ""]
    if rep.summary:
        lines += ["## Summary", "", rep.summary, f"", f"_({rep.summary_source})_", ""]
    lines += ["## Likely causes", ""] + [f"{i}. {c}" for i, c in enumerate(rep.likely_causes, 1)]
    lines += ["", "## Look at next", ""] + [f"- {n}" for n in rep.look_next]
    lines += ["", "## Facts", "", "```json", json.dumps(rep.facts, indent=2, default=str), "```",
              "", "## Steps", ""] + [f"- {s}" for s in rep.steps]
    if rep.trace_id:
        lines += ["", f"Trace: `{rep.trace_id}`"]
    return "\n".join(lines) + "\n"


def events_from_log(log: list[dict]) -> list[dict]:
    """Anomaly events rebuilt from the log (batch mode: no SQL)."""
    out = []
    for r in log:
        if r.get("anomaly"):
            out.append({"fingerprint": r["fingerprint"], "ts": r["ts"],
                        "kind": "size" if r["anomaly"].startswith("adaptive anomaly: rows") else "runtime",
                        "reason": r["anomaly"], "duration_sec": r.get("duration_sec"),
                        "size": r.get("anomaly_size"), "workload_class": r.get("workload_class")})
    return out
