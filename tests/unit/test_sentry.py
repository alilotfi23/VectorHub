"""Sentry error tracking (Phase 7 pull-forward): env gating and correlation.

Pins the contract from CLAUDE.md: unhandled exceptions (500s only — business
errors are handled and never reach Sentry) are captured when SENTRY_DSN is
set, and each captured event carries the request's ``request_id`` and
``trace_id`` tags — the same IDs every log line and span carry, so the Sentry
event joins the log/trace streams by construction.

The on-path test drives a real FastAPI app (raising route + the tracing
middleware) through httpx's ASGI transport, with the SDK initialized against
a recording transport — hermetic: no network, no Sentry server. The gating
test proves an unset DSN leaves the module fully inert (SDK never imported by
the app's code path, helpers no-op).
"""

from collections.abc import Iterator
from typing import Any

import pytest
import sentry_sdk
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

import app.core.sentry as sentry_module
from app.core.config import get_settings
from app.core.sentry import sentry_bind_context, setup_sentry
from app.core.tracing import derive_trace_id
from app.middleware.tracing import TraceMiddleware


class _RecordingTransport(Transport):
    """Captures every event envelope in memory instead of sending it."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        # The event type lives on the envelope *item*, not in the payload dict
        # (error-event payloads carry breadcrumbs/exception/... — no "type"
        # key), so filter on item.type.
        for item in envelope.items:
            if item.type == "event":
                payload = item.payload.json
                if isinstance(payload, dict):
                    self.events.append(payload)


@pytest.fixture
def sentry_gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_RecordingTransport, Any]]:
    """Sentry on with a recording transport, fully torn down afterward.

    ``setup_sentry()`` reads settings and inits the real SDK; the test injects
    the recording transport by wrapping ``sentry_sdk.init`` (the module the
    app's lazy import resolves to — patching the attribute on it intercepts
    the call). Teardown closes the client and resets the module flag so no
    state leaks to other tests.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "sentry_dsn", "http://public@example.com/1")
    monkeypatch.setattr(settings, "sentry_environment", "test")
    transport = _RecordingTransport()
    original_init = sentry_sdk.init

    def _init_with_transport(**kwargs: Any) -> Any:
        return original_init(**kwargs, transport=transport)

    monkeypatch.setattr(sentry_sdk, "init", _init_with_transport)
    setup_sentry()
    assert sentry_module._enabled, "setup_sentry must enable Sentry when a DSN is set"
    try:
        yield transport, settings
    finally:
        sentry_module._enabled = False
        sentry_sdk.flush()  # drain the recording transport
        sentry_sdk.init(dsn=None)  # deactivate the client so no state leaks


def _boom_app() -> FastAPI:
    """A minimal FastAPI app with the platform's tracing middleware and one
    route that raises — the unhandled-exception path Sentry must capture."""

    app = FastAPI()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    app.add_middleware(TraceMiddleware)
    return app


# --- Env gating ------------------------------------------------------------


def test_setup_sentry_noop_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SENTRY_DSN -> the module stays inert: not enabled, and the helpers
    are safe no-ops (this is the deployment that never imports the SDK)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "sentry_dsn", None)
    setup_sentry()
    assert sentry_module._enabled is False
    # Helpers must not raise or touch any SDK state when off.
    sentry_bind_context("req-1", "a" * 32)


def test_setup_sentry_reads_environment_from_settings(
    sentry_gate: tuple[_RecordingTransport, Any],
) -> None:
    """With a DSN, setup_sentry initializes the SDK with the configured
    environment (falling back to the app environment)."""
    transport, _settings = sentry_gate
    client = sentry_sdk.get_client()
    assert client.options["environment"] == "test"
    assert client.options["dsn"] is not None


# --- Correlation on unhandled exceptions -----------------------------------


async def test_unhandled_exception_carries_request_and_trace_id(
    sentry_gate: tuple[_RecordingTransport, Any],
) -> None:
    """A 500 raised in a route is captured with the request_id/trace_id tags
    matching the client-supplied X-Request-ID and its derived trace ID."""
    transport, _settings = sentry_gate
    request_id = "sent-correlation-abc"
    app = _boom_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        resp = await client.get("/boom", headers={"X-Request-ID": request_id})

    assert resp.status_code == 500
    sentry_sdk.flush()  # envelopes deliver on a background transport thread
    assert len(transport.events) == 1, f"expected one captured event, got {len(transport.events)}"
    event = transport.events[0]
    assert "exception" in event, "the event must carry the exception payload"
    assert event["tags"]["request_id"] == request_id
    assert event["tags"]["trace_id"] == f"{derive_trace_id(request_id):032x}"


async def test_generated_request_id_also_carries_correlation(
    sentry_gate: tuple[_RecordingTransport, Any],
) -> None:
    """Without a client header the platform generates the request ID; the
    captured event must carry that generated ID, so the correlation holds
    for every request, not just header-carrying ones. (The event's own
    request_id is the only observable copy on a 500: Starlette's
    ServerErrorMiddleware sends the error response outside the tracing
    middleware's header wrap, so 500s don't echo X-Request-ID — the event
    tags are the authoritative correlation anyway.)"""
    transport, _settings = sentry_gate
    app = _boom_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        resp = await client.get("/boom")

    assert resp.status_code == 500
    sentry_sdk.flush()  # envelopes deliver on a background transport thread
    assert len(transport.events) == 1
    event = transport.events[0]
    generated = event["tags"]["request_id"]
    assert len(generated) == 32  # generated UUID4 hex
    assert event["tags"]["trace_id"] == f"{derive_trace_id(generated):032x}"
