from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.rbac import Permission
from app.core.security import Principal
from app.db.session import get_session
from app.schemas.auth import (
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyListResponse,
    ApiKeyResponse,
)
from app.services.api_key_service import ApiKeyService

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.post("", response_model=ApiKeyCreatedResponse, status_code=201)
async def create_key(
    body: ApiKeyCreateRequest,
    principal: Principal = Depends(require_permission(Permission.TENANT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreatedResponse:
    key, plaintext = await ApiKeyService(session).create_key(
        principal,
        name=body.name,
        role=body.role,
        expires_at=body.expires_at,
        rate_limit_qps=body.rate_limit_qps,
    )
    # Plaintext is shown exactly once; the response model carries it because
    # the ORM object itself has no such attribute. Validate against the base
    # model (no key field), then add the plaintext.
    base = ApiKeyResponse.model_validate(key)
    return ApiKeyCreatedResponse(**base.model_dump(), key=plaintext)


@router.get("", response_model=ApiKeyListResponse)
async def list_keys(
    limit: int = Query(default=50, ge=1, le=200, description="Page size"),
    cursor: str | None = Query(
        default=None, description="Opaque keyset cursor from a previous page"
    ),
    principal: Principal = Depends(require_permission(Permission.TENANT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyListResponse:
    page = await ApiKeyService(session).list_keys(principal, limit=limit, cursor=cursor)
    return ApiKeyListResponse(
        items=[ApiKeyResponse.model_validate(k) for k in page.items],
        next_cursor=page.next_cursor,
        total=page.total,
    )


@router.delete("/{key_id}", status_code=204)
async def revoke_key(
    key_id: str,
    principal: Principal = Depends(require_permission(Permission.TENANT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await ApiKeyService(session).revoke_key(principal, key_id=key_id)
    return Response(status_code=204)
