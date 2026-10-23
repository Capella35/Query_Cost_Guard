"""Swappable interfaces for private deployment.

Real customers will want to run everything inside their own private environment.
These three seams stay small and swappable:

1. RulesPackage   — the static rules; customer-tuned rule packs plug in here.
2. LLMClient      — customer-hosted model, redacted hosted model, or NullLLM
                    (deterministic rewrites only, no model calls at all).
3. TelemetryExporter — aggregates only, opt-in; never SQL text, never anything
                    derived from query text (fingerprints stay in-customer).

Result comparison returns pass/fail + hash, never rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class RewriteProposal:
    original_sql: str
    rewritten_sql: str
    explanation: str


@dataclass
class ComparisonOutcome:
    passed: bool
    result_hash: str  # hash of the sampled result; rows never leave the warehouse
    detail: str = ""
    input_rows_sampled: int = 0  # verification cost: rows read from the sample...
    input_rows_full: int = 0  # ...vs rows the full query's inputs hold


class RulesPackage(Protocol):
    """Static (Layer 1) rules. Deterministic: findings, never judgments."""

    def check(self, sql: str, metadata: dict) -> list[str]:
        """Return human-readable rule findings for the query."""
        ...


class LLMClient(Protocol):
    """Explains flagged queries and proposes cheaper rewrites.

    Called ONLY when the deterministic gate flags a query.
    """

    def explain_flag(self, sql: str, findings: list[str]) -> str: ...
    def propose_rewrite(self, sql: str, findings: list[str]) -> Optional[RewriteProposal]: ...
    def judge(self, proposal) -> str:
        """Threshold-proposal review from aggregates only: approve|reject|hold."""
        ...


class NullLLM:
    """No-LLM deployment: zero model calls. Deterministic rewrites (the CTE
    fixer in rewrites.py) still run in guard.decide(); this client adds none."""

    def explain_flag(self, sql: str, findings: list[str]) -> str:
        return "Query flagged by static rules: " + "; ".join(findings)

    def propose_rewrite(self, sql: str, findings: list[str]) -> Optional[RewriteProposal]:
        return None

    def judge(self, proposal) -> str:
        proposal.judge_outcome = "no judge"
        return "hold"  # no judge: never auto-approve on an LLM's behalf


class TelemetryExporter(Protocol):
    """Aggregates only, opt-in. Counts and distributions — never SQL text."""

    def export_aggregate(self, name: str, value: float, tags: dict) -> None: ...
