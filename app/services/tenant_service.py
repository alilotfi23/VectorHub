"""Tenant provisioning, access, and member management.

Tenant creation is platform-admin only (self-serve registration creates the
user's own tenant). Reads are scoped: a principal can only see their own
tenant, and a missing OR foreign tenant resolves to the same
TENANT_NOT_FOUND — responses must not act as an existence oracle.

Membership: users belong to exactly one tenant, so members are provisioned
as accounts inside the tenant by an admin/owner (TENANT_MANAGE). A tenant
must always retain at least one owner.
"""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.rbac import Permission, has_permission, role_rank
from app.core.security import Principal, hash_password
from app.db.models import Tenant, User
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
        await self._assert_tenant_access(actor, tenant_id)
        tenant = await self._session.get(Tenant, tenant_id)
        assert tenant is not None
        return tenant

    # --- Members ---

    async def list_members(self, actor: Principal, *, tenant_id: str) -> list[User]:
        await self._assert_tenant_access(actor, tenant_id)
        rows = await self._session.scalars(select(User).where(User.tenant_id == tenant_id))
        # Deterministic order: role rank descending (owners first), then email.
        # created_at is a Python-side default that can tie within a batch, so
        # it must not be the sort key; email is unique, so the key is total.
        return sorted(rows, key=lambda u: (-role_rank(u.role), u.email))

    async def add_member(
        self,
        actor: Principal,
        *,
        tenant_id: str,
        email: str,
        password: str,
        role: str,
    ) -> User:
        await self._assert_tenant_access(actor, tenant_id)
        self._require_manage(actor)
        email = email.strip().lower()
        if await self._session.scalar(select(User).where(User.email == email)):
            raise AppError(
                ErrorCode.AUTH_EMAIL_TAKEN, "Email is already registered", status_code=409
            )
        user = User(
            tenant_id=tenant_id,
            email=email,
            password_hash=hash_password(password),
            role=role,
        )
        self._session.add(user)
        await self._session.flush()
        await self._audit.record(
            tenant_id=tenant_id,
            actor_id=actor.user_id,
            action="member.added",
            resource_type="user",
            resource_id=user.id,
            details={"email": email, "role": role},
        )
        try:
            await self._session.commit()
        except IntegrityError as exc:
            raise AppError(
                ErrorCode.AUTH_EMAIL_TAKEN, "Email is already registered", status_code=409
            ) from exc
        return user

    async def change_member_role(
        self, actor: Principal, *, tenant_id: str, user_id: str, role: str
    ) -> User:
        await self._assert_tenant_access(actor, tenant_id)
        self._require_manage(actor)
        user = await self._session.scalar(
            select(User).where(User.id == user_id, User.tenant_id == tenant_id)
        )
        if user is None:
            raise AppError(ErrorCode.TENANT_MEMBER_NOT_FOUND, "Member not found", status_code=404)
        if user.role == "owner" and role != "owner":
            owner_count = await self._session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.tenant_id == tenant_id, User.role == "owner")
            )
            if owner_count is not None and owner_count <= 1:
                raise AppError(
                    ErrorCode.TENANT_LAST_OWNER,
                    "Tenant must retain at least one owner",
                    status_code=409,
                )
        previous = user.role
        user.role = role
        await self._audit.record(
            tenant_id=tenant_id,
            actor_id=actor.user_id,
            action="member.role_changed",
            resource_type="user",
            resource_id=user.id,
            details={"from_role": previous, "to_role": role},
        )
        await self._session.commit()
        return user

    # --- helpers ---

    async def _assert_tenant_access(self, actor: Principal, tenant_id: str) -> None:
        """Tenant must exist AND belong to the actor (or the actor be a platform
        admin); a foreign tenant is indistinguishable from a missing one."""
        tenant = await self._session.get(Tenant, tenant_id)
        if tenant is None or (tenant.id != actor.tenant_id and not actor.is_platform_admin):
            raise AppError(ErrorCode.TENANT_NOT_FOUND, "Tenant not found", status_code=404)

    def _require_manage(self, actor: Principal) -> None:
        if not has_permission(actor, Permission.TENANT_MANAGE):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                "Admin or owner role required to manage members",
                status_code=403,
            )
