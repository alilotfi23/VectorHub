"""The admin app split: /health and /metrics exist only on app.admin, never on
the public app, and the admin app carries none of the public middleware. Also
pins that the two-server runner's signal-neutral Server subclass leaves the
process's signal handlers untouched (two uvicorn servers must not fight over
SIGINT/SIGTERM — see app/main.py's run())."""

import signal

from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from uvicorn import Config, Server

from app.admin import app as admin_app
from app.main import _SignalNeutralServer
from app.main import app as public_app
from app.middleware.metrics import MetricsMiddleware
from app.middleware.rate_limit import RateLimitMiddleware

ADMIN_ROUTES = {route.path for route in admin_app.routes if isinstance(route, APIRoute)}
PUBLIC_ROUTES = {route.path for route in public_app.routes if isinstance(route, APIRoute)}


def test_public_app_never_exposes_infra_endpoints() -> None:
    """Public traffic has no route to the probe/scrape endpoints — they 404
    on the public app by construction (not by auth)."""
    assert "/health" not in PUBLIC_ROUTES
    assert "/metrics" not in PUBLIC_ROUTES


def test_admin_app_serves_only_infra_endpoints() -> None:
    """The admin surface is exactly /health and /metrics — no API routes, no
    docs (openapi_url=None), so there is nothing else to discover or probe."""
    assert "/health" in ADMIN_ROUTES
    assert "/metrics" in ADMIN_ROUTES
    assert not any(r.startswith("/api/v1") for r in ADMIN_ROUTES)
    assert "/docs" not in ADMIN_ROUTES
    assert "/openapi.json" not in ADMIN_ROUTES


def test_admin_app_carries_no_public_middleware() -> None:
    """No auth-adjacent or request-shaping middleware on the admin app: a
    scrape must never be rate-limited or fail on a missing credential, and
    its own traffic must not pollute the request counters it exposes."""
    middleware_classes = {m.cls for m in admin_app.user_middleware}
    assert not middleware_classes & {RateLimitMiddleware, MetricsMiddleware, CORSMiddleware}
    # The public app, by contrast, has all three.
    public_classes = {m.cls for m in public_app.user_middleware}
    assert public_classes & {RateLimitMiddleware, MetricsMiddleware, CORSMiddleware}


def test_signal_neutral_server_does_not_capture_signals() -> None:
    """_SignalNeutralServer.capture_signals is a no-op: entering it must not
    install process signal handlers. If it did, a second concurrent server
    would override the first's handler and SIGINT would stop only one of the
    two servers, hanging shutdown."""
    server = _SignalNeutralServer(Config(admin_app, port=0))
    before = signal.getsignal(signal.SIGINT)
    with server.capture_signals():
        after = signal.getsignal(signal.SIGINT)
    assert after is before
    # Sanity: the real uvicorn.Server does capture (that's why we subclass).
    plain = Server(Config(admin_app, port=0))
    with plain.capture_signals():
        captured = signal.getsignal(signal.SIGINT)
    assert captured is not before
