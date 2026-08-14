from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.rbac import Permission
from app.core.security import Principal
from app.db.session import get_session
from app.schemas.auth import ApiKeyCreatedResponse, ApiKeyCreateRequest, ApiKeyResponse
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


@router.get("", response_model=list[ApiKeyResponse])
async def list_keys(
    principal: Principal = Depends(require_permission(Permission.TENANT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> list[ApiKeyResponse]:
    keys = await ApiKeyService(session).list_keys(principal)
    return [ApiKeyResponse.model_validate(k) for k in keys]


@router.delete("/{key_id}", status_code=204)
async def revoke_key(
    key_id: str,
    principal: Principal = Depends(require_permission(Permission.TENANT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await ApiKeyService(session).revoke_key(principal, key_id=key_id)
    return Response(status_code=204)
