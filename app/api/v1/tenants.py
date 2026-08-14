from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission, require_platform_admin
from app.core.rbac import Permission
from app.core.security import Principal
from app.db.session import get_session
from app.schemas.auth import (
    MemberCreateRequest,
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


@router.get("/{tenant_id}/members", response_model=list[MemberResponse])
async def list_members(
    tenant_id: str,
    principal: Principal = Depends(require_permission(Permission.TENANT_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[MemberResponse]:
    members = await TenantService(session).list_members(principal, tenant_id=tenant_id)
    return [MemberResponse.model_validate(m) for m in members]


@router.post("/{tenant_id}/members", response_model=MemberResponse, status_code=201)
async def add_member(
    tenant_id: str,
    body: MemberCreateRequest,
    principal: Principal = Depends(require_permission(Permission.TENANT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> MemberResponse:
    user = await TenantService(session).add_member(
        principal,
        tenant_id=tenant_id,
        email=body.email,
        password=body.password,
        role=body.role,
    )
    return MemberResponse.model_validate(user)


@router.patch("/{tenant_id}/members/{user_id}", response_model=MemberResponse)
async def change_member_role(
    tenant_id: str,
    user_id: str,
    body: MemberRoleUpdateRequest,
    principal: Principal = Depends(require_permission(Permission.TENANT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> MemberResponse:
    user = await TenantService(session).change_member_role(
        principal, tenant_id=tenant_id, user_id=user_id, role=body.role
    )
    return MemberResponse.model_validate(user)
