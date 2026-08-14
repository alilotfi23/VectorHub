"""Redis-backed rate limiting middleware.

Every request under ``/api/v1/*`` is checked against all applicable limits
— route (always), tenant and API key (when the request authenticates) — and
the **most restrictive wins**: limits are consumed in order (route, API key,
tenant) and the first rejection answers 429 with a ``Retry-After`` header and
an error body whose ``details.limit`` names the limit hit (``route_qps``,
``api_key_qps``, ``tenant_qps``) per the RATE_LIMIT_* taxonomy.

The principal for tenant/key limits is derived only from the presented
credentials (Bearer JWT decode, or API-key authenticate) and never from the
body; invalid/expired credentials simply skip the tenant/key limits and the
route's own auth dependency produces the 401 as usual — auth semantics are
untouched. Rate limits fail open when Redis (or the config read-through) is
unavailable: the limiter is a mitigation, not an availability gate.

The middleware resolves tenant/key rate config through ``session_factory``
(default: the app's session factory) — overridable in tests, mirroring how
tests override ``get_session`` for routes.
"""

import asyncio
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.cache import get_redis
from app.core.exceptions import AppError, ErrorCode, ErrorResponse
from app.core.logging import get_logger
from app.core.metrics import rate_limit_rejection
from app.core.rate_limit import (
    RATE_LIMIT_DB_TIMEOUT_SECONDS,
    RL_KEY_PREFIX,
    RL_ROUTE_PREFIX,
    RL_TENANT_PREFIX,
    LimitKind,
    consume_token,
    resolve_api_key_rate,
    resolve_tenant_rate,
    route_rate,
)
from app.core.security import Principal, decode_access_token
from app.db.session import SessionLocal
from app.services.api_key_service import ApiKeyService

API_KEY_HEADER = "X-API-Key"

logger = get_logger("rate_limit")


@dataclass(frozen=True)
class Rejection:
    """Which limit rejected the request, for the log line and the counter."""

    kind: LimitKind
    retry_after: int
    tenant_id: str | None = None
    api_key_id: str | None = None


# Overridable for tests (see module docstring).
session_factory = SessionLocal

_LIMIT_ERROR_CODES: dict[LimitKind, ErrorCode] = {
    "route_qps": ErrorCode.RATE_LIMIT_ROUTE_QPS,
    "api_key_qps": ErrorCode.RATE_LIMIT_API_KEY_QPS,
    "tenant_qps": ErrorCode.RATE_LIMIT_TENANT_QPS,
}


class RateLimitMiddleware:
    """ASGI middleware enforcing token-bucket limits on /api/v1/* requests."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if not request.url.path.startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return

        rejected = await _decision(request)
        if rejected is not None:
            _record_rejection(request.method, request.url.path, rejected)
            await _send_rate_limited(scope, receive, send, rejected.kind, rejected.retry_after)
            return
        await self.app(scope, receive, send)


async def _decision(request: Request) -> Rejection | None:
    """Consume one token per applicable limit; first rejection wins."""
    redis = get_redis()

    # Route limit applies to every request, authenticated or not.
    route_key = f"{request.method} {request.url.path}"
    allowed, retry_after = await consume_token(
        redis, f"{RL_ROUTE_PREFIX}{route_key}", route_rate(route_key)
    )
    if not allowed:
        return Rejection("route_qps", retry_after)

    auth_header = request.headers.get("Authorization", "")
    api_key = request.headers.get(API_KEY_HEADER)
    if not auth_header and api_key is None:
        return None  # unauthenticated: route limit only

    principal: Principal | None = None
    try:
        async with session_factory() as session:
            if auth_header.lower().startswith("bearer "):
                try:
                    principal = decode_access_token(auth_header[7:].strip()).principal
                except AppError:
                    return None  # the route's auth dependency will 401
            elif api_key is not None:
                principal = await asyncio.wait_for(
                    ApiKeyService(session).authenticate(api_key),
                    timeout=RATE_LIMIT_DB_TIMEOUT_SECONDS,
                )
                if principal is None:
                    return None  # invalid key: the route's auth dependency 401s
            else:
                return None  # unknown credential scheme — the route 401s

            if principal.api_key_id is not None:
                key_rate = await resolve_api_key_rate(redis, session, principal.api_key_id)
                if key_rate is not None:
                    allowed, retry_after = await consume_token(
                        redis, f"{RL_KEY_PREFIX}{principal.api_key_id}", key_rate
                    )
                    if not allowed:
                        return Rejection(
                            "api_key_qps",
                            retry_after,
                            tenant_id=principal.tenant_id,
                            api_key_id=principal.api_key_id,
                        )

            tenant_rate = await resolve_tenant_rate(redis, session, principal.tenant_id)
            if tenant_rate is not None:
                allowed, retry_after = await consume_token(
                    redis, f"{RL_TENANT_PREFIX}{principal.tenant_id}", tenant_rate
                )
                if not allowed:
                    return Rejection(
                        "tenant_qps",
                        retry_after,
                        tenant_id=principal.tenant_id,
                        api_key_id=principal.api_key_id,
                    )
    except Exception:
        # Fail open: config/DB errors must never break the API.
        return None
    return None


def _record_rejection(method: str, path: str, rejection: Rejection) -> None:
    """Make the rejection observable: a counter plus a structured log line
    naming the limit hit. No secrets — only ids and the request's method/path."""
    rate_limit_rejection(rejection.kind)
    logger.warning(
        "rate_limit_exceeded",
        limit=rejection.kind,
        retry_after=rejection.retry_after,
        method=method,
        path=path,
        tenant_id=rejection.tenant_id,
        api_key_id=rejection.api_key_id,
    )


async def _send_rate_limited(
    scope: Scope, receive: Receive, send: Send, kind: LimitKind, retry_after: int
) -> None:
    response = JSONResponse(
        status_code=429,
        content=ErrorResponse(
            error_code=_LIMIT_ERROR_CODES[kind],
            message="Rate limit exceeded",
            details={"limit": kind},
        ).model_dump(mode="json"),
        headers={"Retry-After": str(retry_after)},
    )
    await response(scope, receive, send)
