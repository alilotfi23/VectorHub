from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_principal,
    require_permission,
    require_platform_admin,
    require_tenant_access,
)
from app.core.rbac import Permission
from app.core.security import Principal
from app.db.models import Tenant
from app.db.session import get_session
from app.schemas.auth import (
    MemberCreateRequest,
    MemberListResponse,
    MemberResponse,
    MemberRoleUpdateRequest,
    TenantCreateRequest,
    TenantResponse,
)
from app.services.tenant_service import TenantService

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.post("", response_model=TenantResponse, status_code=201)
async def create_tenant(
    body: TenantCreateRequest,
    principal: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> TenantResponse:
    tenant = await TenantService(session).create_tenant(
        principal, name=body.name, rate_limit_qps=body.rate_limit_qps
    )
    return TenantResponse.model_validate(tenant)


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: str,
    principal: Principal = Depends(require_permission(Permission.TENANT_READ)),
    session: AsyncSession = Depends(get_session),
) -> TenantResponse:
    tenant = await TenantService(session).get_tenant(principal, tenant_id=tenant_id)
    return TenantResponse.model_validate(tenant)


@router.get("/{tenant_id}/members", response_model=MemberListResponse)
async def list_members(
    limit: int = Query(default=50, ge=1, le=200, description="Page size"),
    cursor: str | None = Query(
        default=None, description="Opaque keyset cursor from a previous page"
    ),
    tenant: Tenant = Depends(require_tenant_access(Permission.TENANT_READ)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> MemberListResponse:
    page = await TenantService(session).list_members(
        principal, tenant=tenant, limit=limit, cursor=cursor
    )
    return MemberListResponse(
        items=[MemberResponse.model_validate(m) for m in page.items],
        next_cursor=page.next_cursor,
        total=page.total,
    )


@router.post("/{tenant_id}/members", response_model=MemberResponse, status_code=201)
async def add_member(
    body: MemberCreateRequest,
    tenant: Tenant = Depends(require_tenant_access(Permission.TENANT_MANAGE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> MemberResponse:
    user = await TenantService(session).add_member(
        principal,
        tenant=tenant,
        email=body.email,
        password=body.password,
        role=body.role,
    )
    return MemberResponse.model_validate(user)


@router.patch("/{tenant_id}/members/{user_id}", response_model=MemberResponse)
async def change_member_role(
    user_id: str,
    body: MemberRoleUpdateRequest,
    tenant: Tenant = Depends(require_tenant_access(Permission.TENANT_MANAGE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> MemberResponse:
    user = await TenantService(session).change_member_role(
        principal, tenant=tenant, user_id=user_id, role=body.role
    )
    return MemberResponse.model_validate(user)
