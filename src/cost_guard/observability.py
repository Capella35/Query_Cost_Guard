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

Content mode (setup(content=True); only eval/run_eval.py --phoenix-content
turns it on, on TheLook): add_content() also attaches the SQL, the verdict
and the model's prompt and reply as OpenInference input/output attributes —
to the EXPORTED spans only, never to the JSONL trace. Such traces go to
their own Phoenix project and carry `cost_guard.content = included`.
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
_content = False  # content mode: SQL / prompts on exported spans (evals only)


def enabled() -> bool:
    return _tracer is not None


def _endpoint(endpoint: Optional[str]) -> str:
    ep = endpoint or os.environ.get("PHOENIX_COLLECTOR_ENDPOINT") or DEFAULT_ENDPOINT
    return ep if ep.rstrip("/").endswith("/v1/traces") else ep.rstrip("/") + "/v1/traces"


def setup(endpoint: Optional[str] = None, exporter=None, set_global: bool = True,
          project: Optional[str] = None, content: bool = False) -> bool:
    """Start exporting spans. exporter: any OpenTelemetry SpanExporter (tests
    pass an in-memory one); default OTLP over HTTP to `endpoint`.
    set_global: also make this the process's provider, so the MCP SDK's
    spans are exported too (it can be set once per process). Returns False
    when the OpenTelemetry SDK is not installed — tracing stays JSONL-only.
    project: the Phoenix project the spans land in. content: see the module
    docstring."""
    global _tracer, _provider, _content
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        return False
    resource = {"service.name": SERVICE_NAME,
                "cost_guard.content": "included" if content else "none"}
    if project:
        resource["openinference.project.name"] = project  # Phoenix's project key
    provider = TracerProvider(resource=Resource.create(resource))
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_endpoint(endpoint))))
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    if set_global:
        trace.set_tracer_provider(provider)
    _provider = provider
    _tracer = provider.get_tracer("cost_guard")
    _content = bool(content)
    return True


def shutdown() -> None:
    """Flush and stop export (call on server exit; spans are batched)."""
    global _tracer, _provider, _content
    if _provider is not None:
        _provider.shutdown()
    _tracer = _provider = None
    _content = False


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


def content_enabled() -> bool:
    return _tracer is not None and _content


def add_content(**attrs: Any) -> None:
    """In content mode, attributes (e.g. input.value / output.value) on the
    current EXPORTED span only; a no-op otherwise. Never reaches the JSONL
    trace, which stays numbers-only."""
    if not content_enabled():
        return
    from opentelemetry import trace

    from .secret_scan import redact_obj

    set_attributes(trace.get_current_span(), redact_obj(attrs))


def current_trace_id() -> Optional[str]:
    """Hex id of the current OpenTelemetry trace (the id Phoenix shows), or None."""
    if _tracer is None:
        return None
    from opentelemetry import trace

    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
