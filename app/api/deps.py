"""Route dependencies: resolve the authenticated Principal and gate permissions.

Principal is derived exclusively from presented credentials (Bearer JWT or
X-API-Key header) — never from request bodies. Route handlers should depend
on get_current_principal or require_permission(...) rather than parsing
headers themselves, so a future jti-revocation check (Phase 6, Redis) slots
in at exactly one place.
"""

from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.rbac import Permission, has_permission
from app.core.security import Principal, decode_access_token
from app.db.models import RevokedToken, Tenant
from app.db.session import get_session
from app.services.api_key_service import ApiKeyService
from app.services.collection_service import CollectionAccess, CollectionService
from app.services.tenant_service import TenantService

API_KEY_HEADER = "X-API-Key"


async def get_current_principal(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Principal:
    auth_header = request.headers.get("Authorization", "")
    principal: Principal
    if auth_header.lower().startswith("bearer "):
        decoded = decode_access_token(auth_header[7:].strip())
        # Jti deny-list: logout revokes the access token immediately. The
        # check is one indexed lookup; Phase 6's Redis cache fronts this
        # table (Postgres remains the source of truth).
        if await session.scalar(select(RevokedToken.id).where(RevokedToken.jti == decoded.jti)):
            raise AppError(ErrorCode.AUTH_TOKEN_REVOKED, "Access token revoked", status_code=401)
        principal = decoded.principal
        request.state.token_jti = decoded.jti
    else:
        api_key = request.headers.get(API_KEY_HEADER)
        if not api_key:
            raise AppError(ErrorCode.AUTH_INVALID_CREDENTIALS, "Not authenticated", status_code=401)
        resolved = await ApiKeyService(session).authenticate(api_key)
        if resolved is None:
            raise AppError(ErrorCode.AUTH_INVALID_CREDENTIALS, "Invalid API key", status_code=401)
        principal = resolved
        request.state.token_jti = None
    request.state.principal = principal  # for middleware (Phase 6 audit, etc.)
    return principal


def require_permission(permission: Permission) -> Callable[[Principal], Principal]:
    """Dependency factory: reject principals lacking `permission` (403)."""

    def checker(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not has_permission(principal, permission):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {permission.value}",
                status_code=403,
            )
        return principal

    return checker


def require_platform_admin(principal: Principal = Depends(get_current_principal)) -> Principal:
    if not principal.is_platform_admin:
        raise AppError(
            ErrorCode.AUTH_INSUFFICIENT_SCOPE, "Platform admin required", status_code=403
        )
    return principal


def require_collection_permission(
    permission: Permission,
) -> Callable[..., Awaitable[CollectionAccess]]:
    """Dependency factory for collection-scoped routes.

    Resolves the collection named in the path within the caller's tenant
    (a missing OR foreign collection raises COLLECTION_NOT_FOUND — no
    existence oracle) and checks `permission` against the principal's
    tenant role elevated by any resource-level grant on that collection.
    The CollectionAccess (collection + caller's grant) is injected so route
    handlers and service methods share one resolution per request instead
    of re-querying.
    """

    async def checker(
        name: str,
        principal: Principal = Depends(get_current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> CollectionAccess:
        return await CollectionService(session).check_access(principal, permission, name=name)

    return checker


def require_tenant_access(permission: Permission) -> Callable[..., Awaitable[Tenant]]:
    """Dependency factory for tenant-scoped routes.

    Resolves the tenant named in the path within the caller's access (a
    missing OR foreign tenant raises TENANT_NOT_FOUND — no existence oracle)
    and checks `permission` against the principal's tenant role. The resolved
    Tenant is injected so route handlers and service methods share one
    resolution per request instead of re-querying.
    """

    async def checker(
        tenant_id: str,
        principal: Principal = Depends(get_current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> Tenant:
        tenant = await TenantService(session).resolve_tenant(principal, tenant_id=tenant_id)
        if not has_permission(principal, permission):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {permission.value}",
                status_code=403,
            )
        return tenant

    return checker
