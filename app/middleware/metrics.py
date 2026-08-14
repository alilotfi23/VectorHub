"""Request observation middleware: access log + request counter.

Wraps every HTTP request (including 429s from the rate-limit middleware,
which it wraps) and, once it completes, records one ``request_completed``
INFO log line (method, templated path, status) and one
``vhk_requests_total`` increment. The route label is resolved *post-routing*:
Starlette's router sets ``scope["route"]`` before the endpoint runs, so after
the inner app returns we can collapse dynamic path segments to the route
template — ``/api/v1/collections/{name}`` — instead of one series per
literal path. Unmatched requests (404s) fall back to the raw path.
"""

from collections.abc import MutableMapping
from typing import Any, cast

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.logging import get_logger
from app.core.metrics import REQUESTS_TOTAL

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

        await self.app(scope, receive, wrapped_send)

        route = cast(dict[str, Any], scope).get("route")
        if route is not None:
            # Route.path is router-relative (it misses the /api/v1 mount
            # prefix), so reconstruct the full template from the matched path
            # and its captured params: /api/v1/tenants/{tenant_id}. This keeps
            # dynamic segments collapsed to a bounded series either way.
            path = scope["path"]
            path_params = cast(dict[str, Any], scope).get("path_params", {})
            for name, value in path_params.items():
                path = path.replace(str(value), "{" + name + "}")
        else:
            path = scope["path"]
        _record_request(scope["method"], path, status_code)


def _record_request(method: str, path: str, status: int) -> None:
    """One access-log line plus one request counter increment per request.

    The path is the post-routing template (dynamic segments collapsed); the
    status is the raw code. No request bodies or secrets are ever logged.
    """
    REQUESTS_TOTAL.labels(method=method, path=path, status=str(status)).inc()
    logger.info("request_completed", method=method, path=path, status=status)
