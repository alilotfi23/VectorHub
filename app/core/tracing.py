"""OpenTelemetry bootstrap (Phase 7 pull-forward: request/trace correlation).

Two responsibilities:

1. **``setup_tracing()``** configures the process-global ``TracerProvider``.
   Export is env-driven: when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, spans
   are batched to it (the standard ``OTEL_EXPORTER_*`` env vars — headers,
   timeout, ... — apply on top); otherwise spans are created and correlated
   but dropped at the exporter boundary, which is the honest default until an
   observability backend is configured. ``service.name`` comes from
   ``APP_NAME`` so traces are attributable per deployment.

2. **``derive_trace_id(request_id)``** is the correlation mechanism: it maps
   a request ID to the 128-bit OTel trace ID deterministically, so the trace
   ID *is* the request ID — a log line carrying ``request_id`` and a span
   carrying the derived trace ID are correlated by construction, no field
   lookup needed. Hex request IDs (UUID4s from this platform, or hex IDs
   supplied by clients) are left-padded to the 32 hex chars a trace ID needs;
   non-hex IDs are hashed so the mapping stays stable. The result is never 0
   (OTel forbids an all-zero trace ID) — on the degenerate collision with 0,
   a random trace ID is substituted so the span is still valid.
"""

import hashlib
import uuid

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.core.config import get_settings

_HEX_CHARS = frozenset("0123456789abcdefABCDEF")
_TRACE_ID_HEX_DIGITS = 32  # 128-bit OTel trace ID


def derive_trace_id(request_id: str) -> int:
    """Deterministically map a request ID to a 128-bit OTel trace ID.

    ``request_id`` is the value echoed as ``X-Request-ID`` — either a UUID4
    hex string this platform generated or whatever the client supplied. Hex
    IDs (length ≤ 32) are left-padded to 32 hex chars, so an already-32-char
    ID maps onto itself; anything else is sha256-hashed into its first 16
    bytes, keeping the mapping deterministic for arbitrary client IDs.
    """
    if (
        request_id
        and len(request_id) <= _TRACE_ID_HEX_DIGITS
        and all(c in _HEX_CHARS for c in request_id)
    ):
        trace_id = int(request_id.rjust(_TRACE_ID_HEX_DIGITS, "0"), 16)
    else:
        digest = hashlib.sha256(request_id.encode("utf-8")).digest()
        trace_id = int.from_bytes(digest[:16], "big")
    if trace_id == 0:  # OTel requires a non-zero trace ID
        return uuid.uuid4().int
    return trace_id


def setup_tracing() -> None:
    """Configure the process-global TracerProvider (idempotent via lru_cache
    on the tracer; safe to call once at startup)."""
    settings = get_settings()
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: settings.app_name}))
    if settings.otel_exporter_otlp_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
        )
    trace.set_tracer_provider(provider)
