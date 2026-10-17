"""LLM components (Phase 3) — Claude through the official Anthropic SDK.

Where the LLM earns its place, and where it is fenced in:

- propose_rewrite (F3.1): called only on a flagged query. Its SQL is never
  offered unless guard.decide() verifies it with compare_results and it is
  read-only and not costlier by estimate. A wrong rewrite costs a sampled
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

Model: claude-opus-5-5 with server-side refusal fallbacks enabled
(fallbacks="default"); a refusal on the final response is treated as "no
output" — no rewrite, no explanation, judge holds.
"""
from __future__ import annotations

from typing import Callable, Literal, Optional

from pydantic import BaseModel

from .interfaces import RewriteProposal

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class RewriteOut(BaseModel):
    rewritten_sql: str
    preserves_results: bool
    explanation: str


class ExplanationOut(BaseModel):
    explanation: str


class ColumnKindOut(BaseModel):
    table: str
    column: str
    kind: Literal["identifier", "quantity", "date", "category", "text"]
    reason: str


class ColumnKindsOut(BaseModel):
    columns: list[ColumnKindOut]


class JudgmentOut(BaseModel):
    decision: Literal["approve", "reject", "hold"]
    reason: str


REWRITE_SYSTEM = """You rewrite analytics SQL (DuckDB dialect) to be cheaper \
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
the original; a rewrite that changes the result is rejected."""

EXPLAIN_SYSTEM = """You explain to a data analyst, in 2-4 plain sentences, why \
their query was flagged by a cost guard and what to change. The findings are \
authoritative — do not contradict them or add new ones. No preamble."""

JUDGE_SYSTEM = """You review proposed per-query-shape runtime thresholds for a \
warehouse cost guard. A threshold kills queries of that shape that run longer \
than it. Each proposal has already passed deterministic gates (below the hard \
cap, tightens by at most 25%, enough runs, not below the baseline p95). You see \
aggregate statistics only.

Approve only if the threshold would sit comfortably above normal runs of this \
shape given ALL the evidence. Reject if it would likely kill legitimate runs \
(heavy or bimodal tail near the threshold, upward drift in the baseline, \
history too short in time to be representative). Hold if the evidence is \
ambiguous and more history is needed. Your decision cannot enforce anything; \
a shadow trial follows an approval."""


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

    def _parse(self, system: str, user: str, schema: type[BaseModel]):
        self.calls += 1
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
        usage = getattr(resp, "usage", None)
        if usage is not None:  # counts only — the prompt holds the query, so it is not traced
            from .tracing import annotate

            annotate(model=self.model, input_tokens=getattr(usage, "input_tokens", None),
                     output_tokens=getattr(usage, "output_tokens", None),
                     cache_read_tokens=getattr(usage, "cache_read_input_tokens", None))
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
        out = self._parse(
            REWRITE_SYSTEM,
            f"Query:\n{sql}\n\nFindings:\n"
            + "\n".join(f"- {f}" for f in findings)
            + f"\n\nTables and columns:\n{self._columns(sql)}",
            RewriteOut,
        )
        if out is None or not out.preserves_results:
            return None
        return RewriteProposal(original_sql=sql, rewritten_sql=out.rewritten_sql,
                               explanation=out.explanation)

    def explain_flag(self, sql: str, findings: list[str]) -> str:
        out = self._parse(
            EXPLAIN_SYSTEM,
            f"Query:\n{sql}\n\nFindings:\n" + "\n".join(f"- {f}" for f in findings),
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

    def judge(self, proposal) -> str:
        """approve | reject | hold, from aggregates only."""
        out = self._parse(JUDGE_SYSTEM, judge_payload(proposal), JudgmentOut)
        if out is None:
            return "hold"
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
