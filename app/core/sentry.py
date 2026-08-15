"""Sentry error tracking (Phase 7 pull-forward, env-gated).

``setup_sentry()`` initializes the Sentry SDK **only** when ``SENTRY_DSN`` is
configured. Unset means the whole module is a no-op: the SDK is imported
lazily inside the gate, so a deployment that never sets the DSN pays nothing
beyond a module flag check per request.

Scope is errors only: the FastAPI integration captures *unhandled*
exceptions (genuine 500s). Business errors are handled by the app's
exception handlers (``AppError`` -> 4xx/409/422, validation -> 422) and
never reach Sentry. Performance tracing (``traces_sample_rate``) is
deliberately off.

Correlation: the tracing middleware owns request/trace correlation (request
ID deterministically mapped to the OTel trace ID). ``sentry_bind_context``
mirrors those two IDs into the request's Sentry isolation scope at the same
point the middleware binds the structlog contextvars, so an exception event
captured during the request carries ``request_id`` and ``trace_id`` tags —
the exact IDs every log line and span carry. A Sentry event, a log line, and
a span for the same request join by construction, no field lookup needed.

Scope cleanup is the FastAPI integration's job, not ours: it wraps every
request in ``with isolation_scope()`` (a fresh scope per request, popped on
completion), and it captures the exception inside that block — so tags set
mid-request survive to the event and die with the scope. Clearing them in
the tracing middleware would be actively wrong: its ``finally`` runs *before*
ServerErrorMiddleware re-raises the exception the integration captures.

The SDK init is process-global, so the internal admin app shares it: a crash
in the admin app is captured too, which is correct — error capture is SDK
behavior, not middleware, and the admin app's middleware-free design is about
not consuming rate-limit budget or polluting its own scrape endpoints, not
about hiding crashes.
"""

from __future__ import annotations

from app.core.config import get_settings

# Process-global: True once setup_sentry() has initialized the SDK. Read by
# the correlation helpers on every request (a flag check — negligible when
# Sentry is off).
_enabled = False


def setup_sentry() -> None:
    """Initialize the Sentry SDK when ``SENTRY_DSN`` is set; no-op otherwise.

    The SDK is imported lazily inside the gate so an unconfigured deployment
    never imports it. Called once at startup (app.main) before the app is
    built, so the FastAPI integration's patches are installed for every app
    in the process. Idempotent: re-calling re-initializes the client (tests
    use this to flip the gate).
    """
    global _enabled
    settings = get_settings()
    if not settings.sentry_dsn:
        _enabled = False
        return
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment or settings.environment,
        integrations=[FastApiIntegration()],
        send_default_pii=False,
    )
    _enabled = True


def sentry_bind_context(request_id: str, trace_id: str) -> None:
    """Mirror the request's correlation IDs into the Sentry scope (no-op when
    Sentry is not initialized). Called by the tracing middleware at the same
    point the structlog contextvars are bound, so Sentry, logs, and spans
    agree on the request's IDs. Cleanup is the integration's per-request
    scope teardown (see the module docstring)."""
    if not _enabled:
        return
    import sentry_sdk

    scope = sentry_sdk.get_isolation_scope()
    scope.set_tag("request_id", request_id)
    scope.set_tag("trace_id", trace_id)
