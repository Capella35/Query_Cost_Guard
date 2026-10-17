"""Optional OpenTelemetry export of Cost Guard traces — e.g. to a local Phoenix.

Off by default: tests, evals and the CLI write JSONL traces only (tracing.py)
and need no OpenTelemetry packages. Turned on by setup(), which the MCP
server calls when PHOENIX_COLLECTOR_ENDPOINT is set (Phoenix's own variable;
http://localhost:6006 for `phoenix serve`). Once on:

- every Tracer span is also an OpenTelemetry span, tagged with an
  OpenInference span kind so Phoenix draws it as a tool call, chain, LLM
  call or agent step;
- the MCP SDK's own per-request spans (it emits them by default) go to the
  same place, with the Guard's spans nested under them.

What is exported is what the JSONL trace holds: span names, timings and
attributes (fingerprints, counts, verdicts, reasons, LLM token counts).
Never SQL text, prompts or result rows — which is why LLM calls are not
auto-instrumented (an instrumentor would export the prompt, and the prompt
holds the query); llm.py records token counts on its span instead.

Self-hosted Phoenix keeps everything on the machine; pointing the endpoint
at a hosted collector sends these attributes there — a deployment decision.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager, nullcontext
from typing import Any, Optional

DEFAULT_ENDPOINT = "http://localhost:6006/v1/traces"
SERVICE_NAME = "cost-guard"

_tracer = None  # OpenTelemetry tracer once setup() ran; None = export off
_provider = None


def enabled() -> bool:
    return _tracer is not None


def _endpoint(endpoint: Optional[str]) -> str:
    ep = endpoint or os.environ.get("PHOENIX_COLLECTOR_ENDPOINT") or DEFAULT_ENDPOINT
    return ep if ep.rstrip("/").endswith("/v1/traces") else ep.rstrip("/") + "/v1/traces"


def setup(endpoint: Optional[str] = None, exporter=None, set_global: bool = True) -> bool:
    """Start exporting spans. exporter: any OpenTelemetry SpanExporter (tests
    pass an in-memory one); default OTLP over HTTP to `endpoint`.
    set_global: also make this the process's provider, so the MCP SDK's
    spans are exported too (it can be set once per process). Returns False
    when the OpenTelemetry SDK is not installed — tracing stays JSONL-only."""
    global _tracer, _provider
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_endpoint(endpoint))))
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    if set_global:
        trace.set_tracer_provider(provider)
    _provider = provider
    _tracer = provider.get_tracer("cost_guard")
    return True


def shutdown() -> None:
    """Flush and stop export (call on server exit; spans are batched)."""
    global _tracer, _provider
    if _provider is not None:
        _provider.shutdown()
    _tracer = _provider = None


def span_kind(name: str) -> str:
    """OpenInference span kind, so Phoenix shows what each span is."""
    if name.startswith("mcp."):
        return "TOOL"
    if name.startswith("llm."):
        return "LLM"
    if name.startswith("agent."):
        return "AGENT"
    return "CHAIN"


@contextmanager
def _span(name: str):
    with _tracer.start_as_current_span(name, attributes={"openinference.span.kind": span_kind(name)}) as s:
        yield s


def otel_span(name: str):
    """An OpenTelemetry span under the current one, or a no-op when off."""
    return _span(name) if _tracer is not None else nullcontext(None)


def _value(v: Any):
    if isinstance(v, (str, bool, int, float)):
        return v
    if isinstance(v, (list, tuple)) and all(isinstance(x, (str, bool, int, float)) for x in v) \
            and len({type(x) for x in v}) <= 1:
        return list(v)
    return json.dumps(v, default=str)[:2000]


def set_attributes(otel_span_obj, attrs: dict) -> None:
    if otel_span_obj is None:
        return
    for k, v in attrs.items():
        if v is not None:
            otel_span_obj.set_attribute(k, _value(v))


def current_trace_id() -> Optional[str]:
    """Hex id of the current OpenTelemetry trace (the id Phoenix shows), or None."""
    if _tracer is None:
        return None
    from opentelemetry import trace

    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
