"""Secrets never pass through Query Preflight (module secret_scan: not the stdlib `secrets`) (owner's rule, 2026-10-18).

Two kinds of match:
- known credential formats (Anthropic / OpenAI / Google API keys, AWS access
  keys, GitHub and Slack tokens, JWTs, private-key blocks);
- the exact values of this process's own secrets: environment variables whose
  name ends in _KEY, _TOKEN, _SECRET or _PASSWORD (e.g. ANTHROPIC_API_KEY
  from .env, QUERY_PREFLIGHT_LOG_KEY). Exact values cannot false-alarm.

Used three ways: the Guard blocks a query that contains a secret BEFORE any
dry run or model call (guard.decide), so it reaches neither the warehouse
nor the LLM; every MCP tool response is redacted (mcp_server); exported
content traces are redacted (observability.add_content). A generic
"password = ..." pattern is deliberately not used: `WHERE token = 'abc'` is
an ordinary filter in SQL.
"""
from __future__ import annotations

import os
import re
from typing import Any

PATTERNS = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("JSON web token", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
_SECRET_ENV = re.compile(r"(_KEY|_TOKEN|_SECRET|_PASSWORD|_API_KEY)$")
MIN_ENV_SECRET_LEN = 12
REDACTED = "[REDACTED]"


def _env_secrets() -> list[tuple[str, str]]:
    return [(name, v) for name, v in os.environ.items()
            if _SECRET_ENV.search(name) and v and len(v) >= MIN_ENV_SECRET_LEN]


def find(text: str) -> list[str]:
    """Kinds of secret in `text` (names only, never the values)."""
    if not text:
        return []
    kinds = [kind for kind, rx in PATTERNS if rx.search(text)]
    kinds += [f"the value of {name}" for name, v in _env_secrets() if v in text]
    return list(dict.fromkeys(kinds))


def redact(text: str) -> str:
    if not text:
        return text
    for name, v in _env_secrets():
        text = text.replace(v, REDACTED)
    for _, rx in PATTERNS:
        text = rx.sub(REDACTED, text)
    return text


def redact_obj(obj: Any) -> Any:
    """Every string inside a JSON-like value, redacted."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact_obj(v) for v in obj)
    return obj
