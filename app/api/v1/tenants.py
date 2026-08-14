from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission, require_platform_admin
from app.core.rbac import Permission
from app.core.security import Principal
from app.db.session import get_session
from app.schemas.auth import TenantCreateRequest, TenantResponse
from app.services.tenant_service import TenantService

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.post("", response_model=TenantResponse, status_code=201)
async def create_tenant(
    body: TenantCreateRequest,
    principal: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> TenantResponse:
    tenant = await TenantService(session).create_tenant(principal, name=body.name)
    return TenantResponse.model_validate(tenant)


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: str,
    principal: Principal = Depends(require_permission(Permission.TENANT_READ)),
    session: AsyncSession = Depends(get_session),
) -> TenantResponse:
    tenant = await TenantService(session).get_tenant(principal, tenant_id=tenant_id)
    return TenantResponse.model_validate(tenant)
