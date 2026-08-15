"""Request-ID + OpenTelemetry correlation middleware (public app only).

Every inbound HTTP request:

1. **gets a request ID** — the ``X-Request-ID`` header if the client supplied
   one (echoed back verbatim), otherwise a fresh UUID4 hex string — echoed on
   the response as ``X-Request-ID``;
2. **is correlated into OpenTelemetry**: the request ID is deterministically
   mapped to the trace ID (``derive_trace_id``), so the trace ID *is* the
   request ID. Log lines and spans are correlated by construction — grep one
   stream for ``request_id``, the other for ``trace_id``, and they match
   without a lookup table;
3. **gets a root span** named ``METHOD /path`` (renamed to the templated
   route post-routing), carrying ``http.request.method``, ``url.path``,
   ``http.route``, ``http.response.status_code``, and ``http.request_id``.
   Exceptions are recorded on the span and re-raised.

The request ID and the 32-hex trace ID are bound into the structlog
contextvars for the request's lifetime (``merge_contextvars`` is already in
the processor chain), so every log line emitted while the request is in
flight — service layer, adapters, Redis, Postgres — carries both. The same
two IDs are mirrored into the Sentry isolation scope (when Sentry is
configured), so an unhandled exception captured during the request carries
matching ``request_id``/``trace_id`` tags. The admin app deliberately has no
middleware; this correlation layer is public-app-only like the rest.
"""

import secrets
import uuid
from collections.abc import MutableMapping
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import (
    NonRecordingSpan,
    Span,
    SpanContext,
    StatusCode,
    TraceFlags,
    set_span_in_context,
    use_span,
)
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.sentry import sentry_bind_context
from app.core.tracing import derive_trace_id
from app.middleware.routing import route_template

REQUEST_ID_HEADER = "x-request-id"
MAX_REQUEST_ID_LENGTH = 128


class TraceMiddleware:
    """ASGI middleware: request-ID echo + root span + log correlation."""

    def __init__(self, app: ASGIApp, *, tracer: trace.Tracer | None = None) -> None:
        self.app = app
        # Injectable for tests; the app's stack resolves the process tracer
        # (set by app.core.tracing.setup_tracing at startup).
        self._tracer = tracer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(scope)
        trace_id = derive_trace_id(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id, trace_id=f"{trace_id:032x}")
        # Same IDs into the Sentry scope (no-op when Sentry is off): an
        # unhandled exception captured during this request carries the
        # request_id/trace_id tags, joining it to the log/trace streams.
        sentry_bind_context(request_id, f"{trace_id:032x}")

        tracer = self._tracer or trace.get_tracer("app.middleware.tracing")
        span = tracer.start_span(
            f"{scope['method']} {scope['path']}", context=_parent_context(trace_id)
        )
        status_code = 0

        try:

            async def wrapped_send(message: MutableMapping[str, Any]) -> None:
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                    MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
                await send(message)

            with use_span(span, end_on_exit=False):
                await self.app(scope, receive, wrapped_send)
        except BaseException as exc:
            span.set_status(StatusCode.ERROR)
            span.record_exception(exc)
            raise
        finally:
            _finalize_span(span, scope, request_id, status_code)
            span.end()
            structlog.contextvars.unbind_contextvars("request_id", "trace_id")


def _resolve_request_id(scope: Scope) -> str:
    """The client's X-Request-ID when present and sane, else a new UUID4."""
    for name, value in scope.get("headers", []):
        if name == REQUEST_ID_HEADER.encode("latin-1"):
            text: str = value.decode("latin-1").strip()
            if text and len(text) <= MAX_REQUEST_ID_LENGTH:
                return text
    return uuid.uuid4().hex


def _parent_context(trace_id: int) -> Context:
    """A non-recording parent carrying the request's trace ID.

    The SDK derives the real root span's trace ID from this parent, so the
    span inherits the request ID by construction; the parent's span_id is a
    throwaway (the SDK assigns the real one to the recorded span).
    """
    span_id = secrets.randbits(64) or 1
    span_context = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return set_span_in_context(NonRecordingSpan(span_context))


def _finalize_span(span: Span, scope: Scope, request_id: str, status_code: int) -> None:
    span.set_attribute("http.request.method", scope["method"])
    span.set_attribute("url.path", scope["path"])
    span.set_attribute("http.response.status_code", status_code)
    span.set_attribute("http.request_id", request_id)
    if scope.get("route") is not None:
        templated = route_template(scope)
        span.update_name(f"{scope['method']} {templated}")
        span.set_attribute("http.route", templated)
