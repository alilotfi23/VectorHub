import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.security import Principal
from app.db.models import AuditLog, User
from app.services.auth_service import AuthService
from app.services.tenant_service import TenantService


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _register(db: AsyncSession, tag: str) -> tuple[User, Principal]:
    user, pair = await AuthService(db).register(
        email=_unique(f"{tag}@example.com"),
        password="password-123",
        tenant_name=_unique(tag),
    )
    return user, Principal(user_id=user.id, tenant_id=user.tenant_id, role=user.role)


async def _register_platform_admin(db: AsyncSession, tag: str) -> tuple[User, Principal]:
    user, _ = await AuthService(db).register(
        email=_unique(f"{tag}@example.com"),
        password="password-123",
        tenant_name=_unique(tag),
    )
    user.is_platform_admin = True
    await db.commit()
    principal = Principal(
        user_id=user.id, tenant_id=user.tenant_id, role=user.role, is_platform_admin=True
    )
    return user, principal


async def test_platform_admin_creates_tenant(db: AsyncSession) -> None:
    admin_user, admin = await _register_platform_admin(db, "admin")
    tenant = await TenantService(db).create_tenant(admin, name=_unique("provisioned"))
    assert tenant.id
    row = await db.scalar(
        select(AuditLog).where(AuditLog.action == "tenant.created", AuditLog.tenant_id == tenant.id)
    )
    assert row is not None
    assert row.actor_id == admin_user.id


async def test_non_admin_cannot_create_tenant(db: AsyncSession) -> None:
    _, owner = await _register(db, "owner")
    with pytest.raises(AppError) as exc:
        await TenantService(db).create_tenant(owner, name=_unique("nope"))
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE
    assert exc.value.status_code == 403


async def test_duplicate_tenant_name(db: AsyncSession) -> None:
    _, admin = await _register_platform_admin(db, "admin")
    name = _unique("dup-tenant")
    svc = TenantService(db)
    await svc.create_tenant(admin, name=name)
    with pytest.raises(AppError) as exc:
        await svc.create_tenant(admin, name=name)
    assert exc.value.code == ErrorCode.TENANT_ALREADY_EXISTS


async def test_member_can_get_own_tenant(db: AsyncSession) -> None:
    user, principal = await _register(db, "member")
    tenant = await TenantService(db).get_tenant(principal, tenant_id=user.tenant_id)
    assert tenant.id == user.tenant_id


async def test_foreign_tenant_looks_missing(db: AsyncSession) -> None:
    # No existence oracle: a tenant in another org resolves to TENANT_NOT_FOUND.
    _, a = await _register(db, "orga")
    user_b, _ = await _register(db, "orgb")
    with pytest.raises(AppError) as exc:
        await TenantService(db).get_tenant(a, tenant_id=user_b.tenant_id)
    assert exc.value.code == ErrorCode.TENANT_NOT_FOUND
    assert exc.value.status_code == 404

    with pytest.raises(AppError) as exc:
        await TenantService(db).get_tenant(a, tenant_id="does-not-exist")
    assert exc.value.code == ErrorCode.TENANT_NOT_FOUND


async def test_platform_admin_can_get_any_tenant(db: AsyncSession) -> None:
    _, admin = await _register_platform_admin(db, "admin")
    user_b, _ = await _register(db, "orgb")
    tenant = await TenantService(db).get_tenant(admin, tenant_id=user_b.tenant_id)
    assert tenant.id == user_b.tenant_id
