"""Tenant provisioning and access.

Tenant creation is platform-admin only (self-serve registration creates the
user's own tenant). Reads are scoped: a principal can only see their own
tenant, and a missing OR foreign tenant resolves to the same
TENANT_NOT_FOUND — responses must not act as an existence oracle.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.security import Principal
from app.db.models import Tenant
from app.services.audit_service import AuditService


class TenantService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def create_tenant(self, actor: Principal, *, name: str) -> Tenant:
        if not actor.is_platform_admin:
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                "Platform admin required to create tenants",
                status_code=403,
            )
        if await self._session.scalar(select(Tenant).where(Tenant.name == name)):
            raise AppError(
                ErrorCode.TENANT_ALREADY_EXISTS, "Tenant name is already taken", status_code=409
            )
        tenant = Tenant(name=name)
        self._session.add(tenant)
        await self._session.flush()
        await self._audit.record(
            tenant_id=tenant.id,
            actor_id=actor.user_id,
            action="tenant.created",
            resource_type="tenant",
            resource_id=tenant.id,
        )
        try:
            await self._session.commit()
        except IntegrityError as exc:
            raise AppError(
                ErrorCode.TENANT_ALREADY_EXISTS, "Tenant name is already taken", status_code=409
            ) from exc
        return tenant

    async def get_tenant(self, actor: Principal, *, tenant_id: str) -> Tenant:
        tenant = await self._session.get(Tenant, tenant_id)
        if tenant is None or (tenant.id != actor.tenant_id and not actor.is_platform_admin):
            raise AppError(ErrorCode.TENANT_NOT_FOUND, "Tenant not found", status_code=404)
        return tenant
