"""Request-level audit middleware — records failed write attempts.

Service-level audits (``AuditService.record``) cover every write SUCCESS with
rich resource context. The gap they leave is failures: a service raises
before it records, so a failed collection/vector/role write was never
audited. This middleware closes that gap by recording one row per failed
*mutating* request (POST/PATCH/DELETE) that carried a decodable principal —
the request is logged with (tenant, actor, action=method+route, resource,
result="failure", status, error_code from the error envelope) into the same
append-only ``audit_log`` table.

Deliberately narrow, to avoid noise and double-recording:
- only mutating methods (GET/HEAD/OPTIONS are read paths; their failures are
  not write attempts);
- only 4xx/5xx outcomes (successes are already audited service-side);
- only where a Bearer JWT principal is present (401/403-auth-noise and
  API-key requests are skipped — 401 has no tenant, and API-key principals
  would need a DB lookup this middleware deliberately avoids). 429
  rate-limit rejections are skipped too: a rejection never reached the write
  path and auditing them would flood the table.

The audit write itself never fails the request: the middleware swallows and
logs any error so auditing stays transparent (the guard trigger/grants keep
the table immutable regardless).

The session factory is injectable (tests point it at the migrated test DB;
the production default is the app's SessionLocal).
"""

from __future__ import annotations

import json
from collections.abc import MutableMapping
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.logging import get_logger
from app.core.security import decode_access_token
from app.db.models import AuditLog
from app.db.session import SessionLocal
from app.middleware.routing import route_template

logger = get_logger("audit")

# Module-level so tests can point the middleware at the migrated test DB
# (same seam as the rate-limit middleware); the production default is the
# app's SessionLocal.
session_factory: async_sessionmaker[Any] = SessionLocal

_MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
# Statuses that represent a genuine failed write attempt with a decodable
# principal (auth rejections and rate-limit 429s are noise by design — see
# the module docstring).
_AUDITED_STATUSES = frozenset({400, 404, 409, 415, 422, 500, 503})


def _principal_from_scope(scope: Scope) -> tuple[str | None, str | None]:
    """Best-effort principal from the Authorization header (JWT claims only —
    no DB, no API-key lookup). Returns (tenant_id, user_id)."""
    headers = dict(scope.get("headers") or [])
    auth = headers.get(b"authorization", b"")
    if not auth.lower().startswith(b"bearer "):
        return None, None
    try:
        decoded = decode_access_token(auth[7:].strip().decode())
    except Exception:
        return None, None
    return decoded.principal.tenant_id, decoded.principal.user_id


class AuditMiddleware:
    """ASGI middleware recording failed mutating requests into audit_log."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in _MUTATING_METHODS:
            await self.app(scope, receive, send)
            return

        status_code = 0
        body = bytearray()

        async def wrapped_send(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            # Buffer only failure bodies (small error envelopes).
            if message["type"] == "http.response.body" and 400 <= status_code < 600:
                body.extend(message.get("body", b""))
            await send(message)

        await self.app(scope, receive, wrapped_send)

        if status_code not in _AUDITED_STATUSES:
            return
        tenant_id, actor_id = _principal_from_scope(scope)
        if tenant_id is None:
            return

        error_code: str | None = None
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
            error_code = payload.get("error_code") if isinstance(payload, dict) else None
        except (ValueError, UnicodeDecodeError):
            pass

        try:
            async with session_factory() as session:
                session.add(
                    AuditLog(
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        action=f"{scope['method']} {route_template(scope)}",
                        resource_type="http",
                        resource_id=None,
                        details={
                            "status": status_code,
                            "error_code": error_code,
                            "path": route_template(scope),
                        },
                        result="failure",
                    )
                )
                await session.commit()
        except Exception:
            # Auditing must never break the request it observes.
            logger.warning("audit_write_failed", status=status_code, exc_info=True)
