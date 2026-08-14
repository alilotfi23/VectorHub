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


# --- Members ---


async def test_add_member_creates_user_in_tenant(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    member = await TenantService(db).add_member(
        owner,
        tenant_id=owner.tenant_id,
        email=_unique("dev@example.com"),
        password="password-123",
        role="editor",
    )
    assert member.tenant_id == owner.tenant_id
    assert member.role == "editor"
    assert member.password_hash != "password-123"  # hashed at rest

    row = await db.scalar(
        select(AuditLog).where(AuditLog.action == "member.added", AuditLog.resource_id == member.id)
    )
    assert row is not None
    assert row.actor_id == owner.user_id
    assert row.details == {"email": member.email, "role": "editor"}


async def test_add_member_duplicate_email(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    email = _unique("dup@example.com")
    svc = TenantService(db)
    await svc.add_member(
        owner, tenant_id=owner.tenant_id, email=email, password="password-123", role="viewer"
    )
    with pytest.raises(AppError) as exc:
        await svc.add_member(
            owner, tenant_id=owner.tenant_id, email=email, password="password-123", role="viewer"
        )
    assert exc.value.code == ErrorCode.AUTH_EMAIL_TAKEN


async def test_add_member_email_in_other_tenant(db: AsyncSession) -> None:
    _, owner_a = await _register(db, "orga")
    user_b, _ = await _register(db, "orgb")
    with pytest.raises(AppError) as exc:
        await TenantService(db).add_member(
            owner_a,
            tenant_id=owner_a.tenant_id,
            email=user_b.email,
            password="password-123",
            role="viewer",
        )
    assert exc.value.code == ErrorCode.AUTH_EMAIL_TAKEN


async def test_add_member_cross_tenant_hidden(db: AsyncSession) -> None:
    _, owner_a = await _register(db, "orga")
    user_b, _ = await _register(db, "orgb")
    # Acting on a foreign tenant looks like the tenant doesn't exist.
    with pytest.raises(AppError) as exc:
        await TenantService(db).add_member(
            owner_a,
            tenant_id=user_b.tenant_id,
            email=_unique("x@example.com"),
            password="password-123",
            role="viewer",
        )
    assert exc.value.code == ErrorCode.TENANT_NOT_FOUND


async def test_add_member_requires_manage(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    editor = Principal(user_id=owner.user_id, tenant_id=owner.tenant_id, role="editor")
    with pytest.raises(AppError) as exc:
        await TenantService(db).add_member(
            editor,
            tenant_id=owner.tenant_id,
            email=_unique("v@example.com"),
            password="password-123",
            role="viewer",
        )
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE
    assert exc.value.status_code == 403


async def test_list_members(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    svc = TenantService(db)
    await svc.add_member(
        owner,
        tenant_id=owner.tenant_id,
        email=_unique("d1@example.com"),
        password="password-123",
        role="editor",
    )
    await svc.add_member(
        owner,
        tenant_id=owner.tenant_id,
        email=_unique("d2@example.com"),
        password="password-123",
        role="viewer",
    )
    members = await svc.list_members(owner, tenant_id=owner.tenant_id)
    assert {m.role for m in members} == {"owner", "editor", "viewer"}
    assert len(members) == 3


async def test_list_members_cross_tenant_hidden(db: AsyncSession) -> None:
    _, owner_a = await _register(db, "orga")
    user_b, _ = await _register(db, "orgb")
    with pytest.raises(AppError) as exc:
        await TenantService(db).list_members(owner_a, tenant_id=user_b.tenant_id)
    assert exc.value.code == ErrorCode.TENANT_NOT_FOUND


async def test_change_member_role(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    svc = TenantService(db)
    member = await svc.add_member(
        owner,
        tenant_id=owner.tenant_id,
        email=_unique("d@example.com"),
        password="password-123",
        role="viewer",
    )
    updated = await svc.change_member_role(
        owner, tenant_id=owner.tenant_id, user_id=member.id, role="admin"
    )
    assert updated.role == "admin"

    row = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "member.role_changed", AuditLog.resource_id == member.id
        )
    )
    assert row is not None
    assert row.details == {"from_role": "viewer", "to_role": "admin"}


async def test_change_member_role_foreign_user(db: AsyncSession) -> None:
    _, owner_a = await _register(db, "orga")
    user_b, _ = await _register(db, "orgb")
    # A user from another tenant is not a member here: looks like not-found.
    with pytest.raises(AppError) as exc:
        await TenantService(db).change_member_role(
            owner_a, tenant_id=owner_a.tenant_id, user_id=user_b.id, role="viewer"
        )
    assert exc.value.code == ErrorCode.TENANT_MEMBER_NOT_FOUND


async def test_last_owner_cannot_be_demoted(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    assert owner.user_id is not None
    with pytest.raises(AppError) as exc:
        await TenantService(db).change_member_role(
            owner, tenant_id=owner.tenant_id, user_id=owner.user_id, role="viewer"
        )
    assert exc.value.code == ErrorCode.TENANT_LAST_OWNER
    assert exc.value.status_code == 409


async def test_second_owner_can_be_demoted(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    svc = TenantService(db)
    co_owner = await svc.add_member(
        owner,
        tenant_id=owner.tenant_id,
        email=_unique("co@example.com"),
        password="password-123",
        role="owner",
    )
    updated = await svc.change_member_role(
        owner, tenant_id=owner.tenant_id, user_id=co_owner.id, role="admin"
    )
    assert updated.role == "admin"


async def test_member_can_login_with_provisioned_password(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    member = await TenantService(db).add_member(
        owner,
        tenant_id=owner.tenant_id,
        email=_unique("login@example.com"),
        password="password-123",
        role="viewer",
    )
    user, _ = await AuthService(db).login(email=member.email, password="password-123")
    assert user.id == member.id
    assert user.role == "viewer"
