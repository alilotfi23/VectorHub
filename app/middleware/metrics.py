"""Request observation middleware: access log + request counter.

Wraps every HTTP request (including 429s from the rate-limit middleware,
which it wraps) and, once it completes, records one ``request_completed``
log line (method, templated path, status, duration_ms) and one
``vhk_requests_total`` increment. The route label is resolved *post-routing*
via ``route_template`` (see middleware/routing.py), collapsing dynamic path
segments to the route template — ``/api/v1/collections/{name}`` — instead of
one series per literal path. Unmatched requests (404s) fall back to the raw
path.

Requests slower than ``slow_request_threshold_ms`` log at WARNING with
``slow=true`` so latency outliers surface in the log stream; everything else
logs at INFO. Duration is wall time measured here, so it includes rate
limiting and any middleware inside this one.
"""

import time
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import REQUESTS_TOTAL
from app.middleware.routing import route_template

logger = get_logger("http")


class MetricsMiddleware:
    """ASGI middleware recording one vhk_requests_total increment per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status_code = 0

        async def wrapped_send(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        start = time.perf_counter()
        await self.app(scope, receive, wrapped_send)
        duration_ms = int((time.perf_counter() - start) * 1000)
        _record_request(
            scope["method"],
            route_template(scope),
            status_code,
            duration_ms,
            get_settings().slow_request_threshold_ms,
        )


def _record_request(
    method: str, path: str, status: int, duration_ms: int, slow_threshold_ms: int
) -> None:
    """One access-log line plus one request counter increment per request.

    The path is the post-routing template (dynamic segments collapsed); the
    status is the raw code; duration_ms is wall time in milliseconds. Requests
    at or above ``slow_threshold_ms`` log at WARNING with slow=true — same
    event, so the access-log stream stays uniform and grep-able — everything
    else at INFO. No request bodies or secrets are ever logged.
    """
    REQUESTS_TOTAL.labels(method=method, path=path, status=str(status)).inc()
    slow = duration_ms >= slow_threshold_ms
    if slow:
        logger.warning(
            "request_completed",
            method=method,
            path=path,
            status=status,
            duration_ms=duration_ms,
            slow=True,
        )
    else:
        logger.info(
            "request_completed",
            method=method,
            path=path,
            status=status,
            duration_ms=duration_ms,
            slow=False,
        )
