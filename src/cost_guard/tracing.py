"""Minimal JSONL tracing for Guard decisions — dependency-free.

Every Guard verdict becomes a trace: one root span plus child spans for the
cost estimate and each static rule evaluated. Append traces to a JSONL file
for offline analysis (per-decision latency, checks per decision).

This is the observability half of advanced agentic engineering, and it costs
nothing: no vendor, no sampling service, just JSON lines.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Tracer:
    def __init__(self):
        self.spans: list[dict] = []

    @contextmanager
    def trace(self, name: str, **attrs):
        tid = uuid.uuid4().hex[:16]
        with self.span(tid, name, **attrs):
            yield tid

    @contextmanager
    def span(self, trace_id: str, name: str, **attrs):
        span = {
            "trace_id": trace_id,
            "span_id": uuid.uuid4().hex[:8],
            "name": name,
            "attrs": attrs,
            "t_ms": None,
        }
        t0 = time.time()
        try:
            yield span
        finally:
            span["t_ms"] = round((time.time() - t0) * 1000, 2)
            self.spans.append(span)

    def save(self, path: str | Path) -> int:
        """Append buffered spans as JSONL. Returns spans written."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            for s in self.spans:
                f.write(json.dumps(s) + "\n")
        n = len(self.spans)
        self.spans.clear()
        return n

    def reset(self):
        self.spans.clear()


_default: Tracer | None = None


def get_tracer() -> Tracer:
    global _default
    if _default is None:
        _default = Tracer()
    return _default
