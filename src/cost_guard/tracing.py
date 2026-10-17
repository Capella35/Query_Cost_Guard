"""Tracing for Guard decisions: JSONL by default, Phoenix (OpenTelemetry) optional.

Every Guard verdict becomes a trace: one root span plus child spans for the
cost estimate and each static rule evaluated. Spans are buffered in the
Tracer and appended to a JSONL file for offline analysis (per-decision
latency, checks per decision) — no vendor, no service, just JSON lines.

When observability.setup() has turned on OpenTelemetry export (e.g. to a
local Phoenix), every span is also an OpenTelemetry span, nested under
whatever span is current (an MCP tool call, an investigator step), and the
JSONL trace_id is the OpenTelemetry trace id, so one id finds a decision in
both. Span attributes are fingerprints, counts, verdicts and reasons — never
SQL text or result rows (the query log's boundary).

Thread-safe: spans from many threads go into one buffer under a lock, and
the current trace and span follow each thread's context (contextvars), so a
long-running service can share one Tracer — or, better, use one per request.
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_current_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar("cg_trace", default=None)
_current_span: contextvars.ContextVar[dict | None] = contextvars.ContextVar("cg_span", default=None)
_save_lock = threading.Lock()  # one writer at a time per process for every trace file


class Tracer:
    def __init__(self):
        self.spans: list[dict] = []
        self._lock = threading.Lock()

    @contextmanager
    def trace(self, name: str, **attrs):
        """Root span of a trace. Inside an active trace (e.g. guard.decide
        under an MCP tool call) it is a child span of that trace instead."""
        with self.span(_current_trace.get(), name, **attrs) as span:
            token = _current_trace.set(span["trace_id"])
            try:
                yield span["trace_id"]
            finally:
                _current_trace.reset(token)

    @contextmanager
    def span(self, trace_id: str | None, name: str, **attrs):
        """A span in trace `trace_id`; None = the current trace, or a new one
        outside any (its id is the OpenTelemetry trace id when export is on)."""
        from . import observability

        parent = _current_span.get()
        span = {
            "trace_id": trace_id or _current_trace.get(),
            "span_id": uuid.uuid4().hex[:8],
            "parent_id": parent["span_id"] if parent else None,
            "name": name,
            "attrs": attrs,
            "t_ms": None,
        }
        token = _current_span.set(span)
        t0 = time.time()
        try:
            with observability.otel_span(name) as otel:
                if span["trace_id"] is None:
                    span["trace_id"] = observability.current_trace_id() or uuid.uuid4().hex[:16]
                try:
                    yield span
                finally:
                    # Attributes added inside the block count too (span["attrs"][k] = ...).
                    observability.set_attributes(otel, span["attrs"])
        finally:
            _current_span.reset(token)
            span["t_ms"] = round((time.time() - t0) * 1000, 2)
            with self._lock:
                self.spans.append(span)

    def save(self, path: str | Path) -> int:
        """Append buffered spans as JSONL. Returns spans written."""
        with self._lock:
            spans, self.spans = self.spans, []
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with _save_lock, p.open("a") as f:
            for s in spans:
                f.write(json.dumps(s, default=str) + "\n")
        return len(spans)

    def reset(self):
        with self._lock:
            self.spans.clear()


def annotate(**attrs: Any) -> None:
    """Add attributes to the current span, if any (e.g. LLM token counts
    recorded deep inside a call without passing the span around)."""
    span = _current_span.get()
    if span is not None:
        span["attrs"].update(attrs)


def read_traces(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


_default: Tracer | None = None


def get_tracer() -> Tracer:
    global _default
    if _default is None:
        _default = Tracer()
    return _default
