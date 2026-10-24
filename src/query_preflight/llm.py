"""LLM components (Phase 3) — Claude through the official Anthropic SDK.

Where the LLM earns its place, and where it is fenced in:

- propose_rewrite (F3.1): called only on a warned or blocked query, AFTER
  the verdict (guard.assist). Its SQL is never offered unless it passes the
  rewrite gates (read-only, cheaper, compare_results) — and even then it is
  an offer: the decision stays as decided. A wrong rewrite costs a sampled
  check, never a wrong answer.
- explain_flag (F3.3): plain-language explanation of a warn/block, written
  AFTER the deterministic verdict. It cannot change the verdict.
- classify_columns: what each column means (identifier, quantity, date,
  category, text), offline, from names, types and counts — never values.
  For a human to review; measured before the Guard uses it
  (eval/run_column_kinds.py).
- judge (F3.2): reviews threshold proposals that already passed the
  deterministic gates. It sees aggregates only (counts, percentiles, rule
  names) — never SQL text, never the fingerprint. It may reject or hold,
  never enforce.

NullLLM (interfaces.py) is the no-LLM mode: zero model calls.

Model: claude-haiku-5-5, Anthropic's cheapest (owner's choice 2026-10-18,
over Opus 5.5 and over adding Gemini 2.5 Flash-Lite: about $0.0003 per call,
$0.10 / $0.50 per 1M tokens), with server-side refusal fallbacks enabled
(fallbacks="default"); a refusal on the final response is treated as "no
output" — no rewrite, no explanation, judge holds.
"""
from __future__ import annotations

from typing import Callable, Literal, Optional

from pydantic import BaseModel

from .interfaces import RewriteProposal

MODEL = "claude-haiku-5-5"
# USD per 1M tokens for MODEL (Claude Platform pricing, prompts <= 100k
# tokens, checked 2026-10-18); for the MODELED cost on each LLM span.
PRICE_PER_MTOK = {"input": 0.10, "output": 0.50, "cache_read": 0.01}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class RewriteOut(BaseModel):
    rewritten_sql: str
    preserves_results: bool
    explanation: str


class ExplanationOut(BaseModel):
    explanation: str


class SuggestionOut(BaseModel):
    improved_sql: str
    changes: list[str]          # every change, each saying what it does to the result
    changes_result: bool        # True if any change can alter the rows or values returned
    runtime_note: str           # why it should run faster or cheaper (the model's reasoning)


class ColumnKindOut(BaseModel):
    table: str
    column: str
    kind: Literal["identifier", "quantity", "date", "category", "text"]
    reason: str


class ColumnKindsOut(BaseModel):
    columns: list[ColumnKindOut]


class SummaryOut(BaseModel):
    summary: str


class JudgmentOut(BaseModel):
    decision: Literal["approve", "reject", "hold"]
    reason: str


REWRITE_SYSTEM = """You rewrite analytics SQL ({dialect}) to be cheaper \
while returning EXACTLY the same result set: same rows, same columns, same \
column order, same ORDER BY semantics. You are given the query, the static-rule \
findings that flagged it, and the columns of each table it reads.

Typical fixes: replace SELECT * with only the columns the query needs ONLY when \
that is the full set the caller sees (otherwise the result changes); make a \
filter on a partition column sargable (compare the bare column to a range \
instead of wrapping it in a function); move filters below joins; hoist repeated \
subqueries into a CTE.

If no rewrite preserves the result exactly (for example, the query genuinely \
needs all history, or SELECT * output is what the caller consumes), set \
preserves_results to false and return the original SQL unchanged. Do not \
invent columns or tables. Your rewrite will be executed and compared against \
the original; a rewrite that changes the result is rejected.{dialect_notes}"""

# The dialect the Guard is checking in (sql_ast.current_dialect()), named in
# the rewrite prompt. Until 2026-10-18 the prompt always said DuckDB, and on
# BigQuery a proposal used `created_at - INTERVAL 1 MONTH` (P25), which
# BigQuery rejects; the notes are those measured failures.
DIALECTS = {
    "bigquery": ("BigQuery GoogleSQL dialect",
                 "\n\nBigQuery rules: TIMESTAMP +/- INTERVAL accepts units up to DAY only "
                 "(no MONTH or YEAR: use DATE_SUB / DATE_ADD on a DATE, or TIMESTAMP_SUB "
                 "with DAY or HOUR); compare a TIMESTAMP column with TIMESTAMP literals, "
                 "not DATE ones."),
    "duckdb": ("DuckDB dialect", ""),
}

SUGGEST_SYSTEM = """You improve an analytics SQL query ({dialect}) that a cost \
guard flagged, so it costs less and runs faster. You are given the query, the \
guard's verdict (decision, reason, findings) and the columns of each table.

Goal: fewer bytes billed first (BigQuery bills the columns read, after \
partition pruning, at least 10 MiB per table), then less work (fewer rows \
joined, sorted or grouped). Typical improvements: filter the partition column \
(created_at) to a bounded range; read only the columns needed; aggregate \
before joining; join on keys; add a LIMIT for top-N output. Fix a clear \
mistake you notice (a filter value that cannot match because of case, a sum \
that counts rows twice because of a join), and say so.

You MAY change the result when that is what saves cost (for example a date \
range the original did not have), but then set changes_result to true and \
list every such change in `changes`, saying what it does to the result and \
what the analyst should check (e.g. "adds created_at >= TIMESTAMP_SUB(\
CURRENT_TIMESTAMP(), INTERVAL 90 DAY): results cover the last 90 days only; \
adjust the range"). Keep the analyst's intent: same question, same output \
columns. Do not invent columns or tables. If nothing would make it cheaper or \
more correct, return the original SQL unchanged with an empty `changes` list.
runtime_note: one sentence on why it should be cheaper or faster.{dialect_notes}"""

EXPLAIN_SYSTEM = """You explain to a data analyst, in 2-4 plain sentences, why \
their query was flagged by a cost guard and what to change. You are given the \
decision and its reason first, then the findings and the fixes the guard \
suggests. They are authoritative: explain the reason first (it is why the query \
was blocked or warned), do not contradict them or add new ones. No preamble."""

JUDGE_SYSTEM = """You review proposed per-query-shape runtime thresholds for a \
warehouse cost guard. A threshold kills queries of that shape that run longer \
than it. Each proposal has already passed deterministic gates (below the hard \
cap, tightens by at most 25%, enough runs, not below the baseline p95). You see \
aggregate statistics only.

How a proposal arises: one is made only after a slowdown. The shape's latest \
runs (recent_median_sec) were at least 1.5x its usual median, and the \
threshold is meant to stop slowdowns like that one. So recent runs sitting \
above the new threshold is the intended effect, not a reason to reject. Every \
other statistic (baseline_*, would_block_rate_on_history, \
baseline_trend_ratio, history_span_days) describes the normal runs BEFORE the \
slowdown: judge the threshold against those.

Approve only if the threshold would sit comfortably above normal runs of this \
shape given ALL the evidence. Reject if it would likely kill legitimate runs \
(heavy or bimodal tail near the threshold, upward drift in the baseline, \
history too short in time to be representative). Hold if the evidence is \
ambiguous and more history is needed. Your decision cannot enforce anything; \
a shadow trial follows an approval."""


SUMMARY_SYSTEM = """You write a short incident note (3-5 plain sentences) for a \
data team about one query shape whose latest run broke its own history. You \
are given the investigator's facts and its deterministic diagnosis as JSON \
(aggregates only: no SQL). Say what happened, the most likely cause from the \
diagnosis, and what to look at next. Do not add causes the facts do not \
support; say what is unknown. Nothing will be changed automatically — do not \
recommend enforcement, thresholds or kills. No preamble."""


class ClaudeLLM:
    """LLMClient backed by Claude. `backend` (optional) supplies table
    columns so rewrites reference real columns."""

    def __init__(
        self,
        model: str = MODEL,
        backend=None,
        client=None,
        effort: str = "medium",
        schema_lookup: Optional[Callable[[str], list[str]]] = None,
    ):
        if client is None:
            import anthropic

            from .config import load_dotenv

            load_dotenv()  # ANTHROPIC_API_KEY from the repo's .env, if not already set
            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort
        self.backend = backend
        self.schema_lookup = schema_lookup
        self.calls = 0
        self.last_stop_reason: Optional[str] = None  # of the latest call
        self.last_usd: Optional[float] = None  # MODELED cost of the latest call

    def _parse(self, system: str, user: str, schema: type[BaseModel]):
        self.calls += 1
        self.last_stop_reason, self.last_usd = None, None
        resp = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": self.effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        self.last_stop_reason = getattr(resp, "stop_reason", None)
        usage = getattr(resp, "usage", None)
        if usage is not None:  # counts only — the prompt holds the query, so it is not traced
            from .tracing import annotate

            tin = getattr(usage, "input_tokens", None) or 0
            tout = getattr(usage, "output_tokens", None) or 0
            tcache = getattr(usage, "cache_read_input_tokens", None) or 0
            usd = None
            if self.model == MODEL:  # price known for this model only
                usd = (tin * PRICE_PER_MTOK["input"] + tout * PRICE_PER_MTOK["output"]
                       + tcache * PRICE_PER_MTOK["cache_read"]) / 1e6
            self.last_usd = usd
            annotate(model=self.model, input_tokens=tin, output_tokens=tout, cache_read_tokens=tcache,
                     # OpenInference names, so Phoenix shows tokens (and cost) per call
                     **{"llm.provider": "anthropic", "llm.model_name": self.model,
                        "llm.token_count.prompt": tin + tcache, "llm.token_count.completion": tout,
                        "llm.token_count.total": tin + tcache + tout,
                        "llm.token_count.prompt_details.cache_read": tcache,
                        "llm.cost.total": usd, "query_preflight.llm_usd_modeled": usd})
        from . import observability

        # Content mode (evals on TheLook only): the prompt and reply on the
        # exported span, so Phoenix can show them; never in the JSONL trace.
        if observability.content_enabled():
            out = resp.parsed_output
            observability.add_content(**{
                "input.value": f"{system}\n\n{user}",
                "output.value": ("(refusal)" if resp.stop_reason == "refusal"
                                 else out.model_dump_json() if hasattr(out, "model_dump_json")
                                 else str(out or "")),
                "llm.model_name": self.model})
        if resp.stop_reason == "refusal":
            return None
        return resp.parsed_output

    def _columns(self, sql: str) -> str:
        from . import sql_ast

        tree = sql_ast.try_parse(sql)
        lines = []
        for t in sql_ast.referenced_tables(tree) if tree is not None else []:
            cols = None
            try:
                if self.schema_lookup is not None:
                    cols = self.schema_lookup(t)
                elif self.backend is not None:
                    cols = self.backend.table_metadata(t).get("columns")
            except Exception:  # noqa: BLE001
                cols = None
            lines.append(f"- {t}: {', '.join(cols) if cols else '(unknown)'}")
        return "\n".join(lines) or "(none)"

    def propose_rewrite(self, sql: str, findings: list[str]) -> Optional[RewriteProposal]:
        from . import sql_ast

        name, notes = DIALECTS.get(sql_ast.current_dialect(), DIALECTS["duckdb"])
        out = self._parse(
            REWRITE_SYSTEM.format(dialect=name, dialect_notes=notes),
            f"Query:\n{sql}\n\nFindings:\n"
            + "\n".join(f"- {f}" for f in findings)
            + f"\n\nTables and columns:\n{self._columns(sql)}",
            RewriteOut,
        )
        if out is None or not out.preserves_results:
            return None
        return RewriteProposal(original_sql=sql, rewritten_sql=out.rewritten_sql,
                               explanation=out.explanation)

    def suggest_improvement(self, sql: str, verdict_items: list[str]) -> Optional[SuggestionOut]:
        """An improved query for a warned or blocked one (guard._suggest
        validates and prices it; it is never run or swapped in by itself).
        None when the model returns nothing new."""
        from . import sql_ast

        name, notes = DIALECTS.get(sql_ast.current_dialect(), DIALECTS["duckdb"])
        out = self._parse(
            SUGGEST_SYSTEM.format(dialect=name, dialect_notes=notes),
            f"Query:\n{sql}\n\nGuard verdict:\n" + "\n".join(f"- {f}" for f in verdict_items)
            + f"\n\nTables and columns:\n{self._columns(sql)}",
            SuggestionOut,
        )
        if out is None or not out.improved_sql.strip() or (
                " ".join(out.improved_sql.split()).lower() == " ".join(sql.split()).lower()):
            return None
        return out

    def explain_flag(self, sql: str, findings: list[str]) -> str:
        """`findings`: what the Guard decided and why — guard.explain_inputs()
        puts the decision and reason first, then findings and fix hints."""
        out = self._parse(
            EXPLAIN_SYSTEM,
            f"Query:\n{sql}\n\nGuard verdict:\n" + "\n".join(f"- {f}" for f in findings),
            ExplanationOut,
        )
        if out is None:
            return "Query flagged by static rules: " + "; ".join(findings)
        return out.explanation

    def classify_columns(self, tables: dict) -> dict[tuple[str, str], dict]:
        """(table, column) -> {kind, reason}, from names, types and counts only
        (column_kinds.describe_tables) — never data values. Offline, once per
        column; the result is for a human to review, not read per query."""
        from .column_kinds import CLASSIFY_SYSTEM, describe_tables

        out = self._parse(CLASSIFY_SYSTEM, describe_tables(tables), ColumnKindsOut)
        if out is None:
            return {}
        return {(c.table, c.column): {"kind": c.kind, "reason": c.reason} for c in out.columns}

    def summarize_investigation(self, facts: dict) -> Optional[str]:
        """Plain-language note from an investigation's facts (aggregates only)."""
        import json

        out = self._parse(SUMMARY_SYSTEM, json.dumps(facts, default=str), SummaryOut)
        return out.summary if out is not None else None

    def judge(self, proposal) -> str:
        """approve | reject | hold, from aggregates only. Records on the
        proposal how the call ended (judge_outcome) and its MODELED cost, so a
        refusal or an error, which hold, is never read as a judgment."""
        try:
            out = self._parse(JUDGE_SYSTEM, judge_payload(proposal), JudgmentOut)
        except Exception as e:  # noqa: BLE001 — an error means no output: the judge holds
            proposal.judge_outcome = f"error: {type(e).__name__}"
            return "hold"
        finally:
            proposal.judge_usd = self.last_usd
        if out is None:
            proposal.judge_outcome = ("refused" if self.last_stop_reason == "refusal"
                                      else f"no output ({self.last_stop_reason})")
            return "hold"
        proposal.judge_outcome = "judged"
        proposal.judge_reason = out.reason
        return out.decision


def judge_payload(proposal) -> str:
    """The judge's entire view of a proposal: numbers and rule names. The
    fingerprint (a normalized SQL shape) and any SQL text are excluded."""
    fields = {
        "rule": "per-shape runtime threshold (duration_sec)",
        "old_threshold_sec": proposal.old_threshold,
        "new_threshold_sec": proposal.new_threshold,
        "baseline_runs": proposal.baseline_runs,
        "baseline_p50_sec": proposal.baseline_p50,
        "baseline_p95_sec": proposal.baseline_p95,
        "baseline_p99_sec": proposal.baseline_p99,
        "baseline_max_sec": proposal.baseline_max,
        "recent_median_sec": proposal.recent_median,
        "history_span_days": proposal.history_span_days,
        "baseline_trend_ratio": proposal.baseline_trend_ratio,
        "would_block_rate_on_history": proposal.would_block_rate_on_history,
    }
    return "\n".join(f"{k}: {v}" for k, v in fields.items() if v is not None)
