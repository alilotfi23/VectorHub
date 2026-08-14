"""Request/trace correlation (Phase 7 pull-forward).

Pins the correlation contract from CLAUDE.md: every request gets a request ID
(echoed as X-Request-ID), the request ID deterministically *becomes* the
OpenTelemetry trace ID, and both are bound into the structlog contextvars for
the request's lifetime — so logs and traces correlate by construction.

The middleware is driven with a hand-built ASGI scope (full control over
headers, route, and path params) against a capturing span exporter injected
via the middleware's ``tracer`` parameter — hermetic: no sockets, no Redis,
no touching the process-global tracer provider (the SDK refuses to override
it once set).
"""

import uuid
from collections.abc import Iterator, MutableMapping, Sequence
from typing import Any, cast

import pytest
import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import StatusCode
from starlette.types import Receive, Scope, Send

from app.core.tracing import derive_trace_id
from app.middleware.tracing import REQUEST_ID_HEADER, TraceMiddleware


class _CapturingExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


@pytest.fixture
def capturing_tracer() -> Iterator[tuple[_CapturingExporter, trace.Tracer]]:
    exporter = _CapturingExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter, provider.get_tracer("test-tracer")


async def _run_request(
    tracer: trace.Tracer,
    *,
    method: str = "GET",
    path: str = "/api/v1/collections/my-collection",
    headers: list[tuple[bytes, bytes]] | None = None,
    path_params: dict[str, str] | None = None,
    include_route: bool = True,
    raise_exc: BaseException | None = None,
    captured_context: dict[str, Any] | None = None,
) -> list[MutableMapping[str, Any]]:
    """Drive a fresh TraceMiddleware through one request with a stubbed inner
    app; returns the messages the inner app sent (first = response.start)."""
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("testclient", 50000),
        "server": ("test", 80),
        "state": {},
    }
    if include_route:
        scope["route"] = object()
    if path_params is not None:
        scope["path_params"] = path_params

    sent: list[MutableMapping[str, Any]] = []

    async def inner(_scope: Scope, _receive: Receive, _send: Send) -> None:
        if captured_context is not None:
            captured_context.update(structlog.contextvars.get_contextvars())
        if raise_exc is not None:
            raise raise_exc
        await _send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await _send({"type": "http.response.body", "body": b"ok"})

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    await TraceMiddleware(inner, tracer=tracer)(scope, receive, send)
    return sent


def _response_headers(sent: list[MutableMapping[str, Any]]) -> list[tuple[bytes, bytes]]:
    return cast(list[tuple[bytes, bytes]], sent[0]["headers"])


# --- Request-ID -> trace-ID derivation -------------------------------------


def test_derive_trace_id_is_identity_for_32_hex_chars() -> None:
    """A full 128-bit hex request ID maps onto itself as the trace ID."""
    request_id = uuid.uuid4().hex
    assert len(request_id) == 32
    assert derive_trace_id(request_id) == int(request_id, 16)


def test_derive_trace_id_left_pads_short_hex_ids() -> None:
    """Short hex IDs (e.g. a client's 8-char id) become the low bits of a
    well-formed 128-bit trace ID — deterministic, never raises."""
    assert derive_trace_id("abc123") == int("00000000000000000000000000abc123", 16)
    assert derive_trace_id("deadbeef") == int("000000000000000000000000deadbeef", 16)


def test_derive_trace_id_hashes_non_hex_ids_deterministically() -> None:
    """Arbitrary client IDs (non-hex, too long) still map to a stable, valid
    trace ID, and the derivation never produces the forbidden zero trace ID."""
    assert derive_trace_id("svc-a/req-1") == derive_trace_id("svc-a/req-1")
    assert 1 <= derive_trace_id("x" * 200) < 2**128
    assert 1 <= derive_trace_id("") < 2**128


# --- Middleware: header echo / generation -----------------------------------


async def test_echoes_client_supplied_request_id(
    capturing_tracer: tuple[_CapturingExporter, trace.Tracer],
) -> None:
    exporter, tracer = capturing_tracer
    sent = await _run_request(tracer, headers=[(REQUEST_ID_HEADER.encode(), b"client-given-id-42")])
    assert (REQUEST_ID_HEADER.encode(), b"client-given-id-42") in _response_headers(sent)


async def test_generates_request_id_when_absent(
    capturing_tracer: tuple[_CapturingExporter, trace.Tracer],
) -> None:
    exporter, tracer = capturing_tracer
    sent = await _run_request(tracer)
    echoed = dict(_response_headers(sent))[REQUEST_ID_HEADER.encode()]
    assert len(echoed) == 32  # UUID4 hex


# --- Middleware: span correlation -------------------------------------------


async def test_span_trace_id_is_the_request_id(
    capturing_tracer: tuple[_CapturingExporter, trace.Tracer],
) -> None:
    exporter, tracer = capturing_tracer
    request_id = uuid.uuid4().hex
    await _run_request(tracer, headers=[(REQUEST_ID_HEADER.encode(), request_id.encode())])
    assert len(exporter.spans) == 1
    assert exporter.spans[0].context is not None
    assert exporter.spans[0].context.trace_id == int(request_id, 16)


async def test_span_records_http_attributes_with_templated_route(
    capturing_tracer: tuple[_CapturingExporter, trace.Tracer],
) -> None:
    exporter, tracer = capturing_tracer
    await _run_request(
        tracer, path="/api/v1/collections/my-collection", path_params={"name": "my-collection"}
    )
    attrs = exporter.spans[0].attributes or {}
    assert exporter.spans[0].name == "GET /api/v1/collections/{name}"
    assert attrs["http.request.method"] == "GET"
    assert attrs["url.path"] == "/api/v1/collections/my-collection"
    assert attrs["http.route"] == "/api/v1/collections/{name}"
    assert attrs["http.response.status_code"] == 200
    assert attrs["http.request_id"] != ""


async def test_exception_records_error_status(
    capturing_tracer: tuple[_CapturingExporter, trace.Tracer],
) -> None:
    exporter, tracer = capturing_tracer
    with pytest.raises(RuntimeError):
        await _run_request(tracer, raise_exc=RuntimeError("boom"))
    span = exporter.spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in span.events)


# --- Middleware: structlog correlation --------------------------------------


async def test_request_id_bound_into_log_context_during_request(
    capturing_tracer: tuple[_CapturingExporter, trace.Tracer],
) -> None:
    exporter, tracer = capturing_tracer
    captured: dict[str, Any] = {}
    request_id = "log-correlation-request"
    await _run_request(
        tracer,
        headers=[(REQUEST_ID_HEADER.encode(), request_id.encode())],
        captured_context=captured,
    )
    # During the request every log line carries request_id + the trace id.
    assert captured["request_id"] == request_id
    assert len(captured["trace_id"]) == 32
    assert int(captured["trace_id"], 16) == derive_trace_id(request_id)
    # After the request the context is clean — no leakage into later log lines.
    assert structlog.contextvars.get_contextvars() == {}


async def test_unmatched_request_uses_raw_path(
    capturing_tracer: tuple[_CapturingExporter, trace.Tracer],
) -> None:
    exporter, tracer = capturing_tracer
    await _run_request(tracer, path="/no/such/route", include_route=False)
    span = exporter.spans[0]
    attrs = span.attributes or {}
    assert span.name == "GET /no/such/route"
    assert "http.route" not in attrs
