import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.rbac import Permission
from app.core.security import Principal
from app.db.models import AuditLog, Collection, CollectionPermission, User
from app.services.auth_service import AuthService
from app.services.collection_service import CollectionService
from app.services.tenant_service import TenantService


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _register(db: AsyncSession, tag: str) -> tuple[User, Principal]:
    user, _ = await AuthService(db).register(
        email=_unique(f"{tag}@example.com"),
        password="password-123",
        tenant_name=_unique(tag),
    )
    return user, Principal(user_id=user.id, tenant_id=user.tenant_id, role=user.role)


async def _add_collection(db: AsyncSession, tenant_id: str, name: str = "products") -> Collection:
    collection = Collection(
        tenant_id=tenant_id,
        name=name,
        backend="chroma",
        dimension=8,
        distance_metric="cosine",
        physical_name=f"col_{uuid.uuid4().hex[:12]}",
    )
    db.add(collection)
    await db.commit()
    return collection


async def _add_viewer_member(db: AsyncSession, owner: Principal) -> tuple[User, Principal]:
    member = await TenantService(db).add_member(
        owner,
        tenant_id=owner.tenant_id,
        email=_unique("viewer@example.com"),
        password="password-123",
        role="viewer",
    )
    return member, Principal(user_id=member.id, tenant_id=owner.tenant_id, role="viewer")


async def test_grant_upsert_and_check_access(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    _, viewer = await _add_viewer_member(db, owner)
    collection = await _add_collection(db, owner.tenant_id)
    svc = CollectionService(db)

    # Viewer without a grant: read-only on this collection.
    with pytest.raises(AppError) as exc:
        await svc.check_access(viewer, Permission.COLLECTION_WRITE, name=collection.name)
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE
    assert await svc.check_access(viewer, Permission.COLLECTION_READ, name=collection.name)

    grant = await svc.grant_permission(
        owner, name=collection.name, user_id=viewer.user_id or "", role="editor"
    )
    assert grant.permission == "editor"
    assert grant.collection_id == collection.id
    assert grant.user_id == viewer.user_id

    # Grant elevates: editor on the collection now passes write checks...
    assert await svc.check_access(viewer, Permission.COLLECTION_WRITE, name=collection.name)
    # ...but not beyond the grant.
    with pytest.raises(AppError) as exc:
        await svc.check_access(viewer, Permission.COLLECTION_DELETE, name=collection.name)
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE

    # Upsert: re-granting a different role updates, never duplicates.
    await svc.grant_permission(
        owner, name=collection.name, user_id=viewer.user_id or "", role="viewer"
    )
    count = await db.scalar(
        select(func.count())
        .select_from(CollectionPermission)
        .where(CollectionPermission.collection_id == collection.id)
    )
    assert count == 1
    with pytest.raises(AppError) as exc:
        await svc.check_access(viewer, Permission.COLLECTION_WRITE, name=collection.name)
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE


async def test_tenant_role_sufficient_without_grant(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    svc = CollectionService(db)
    # Owner (and any admin/editor) passes without any resource grant.
    assert await svc.check_access(owner, Permission.COLLECTION_DELETE, name=collection.name)


async def test_foreign_or_missing_collection_looks_the_same(db: AsyncSession) -> None:
    _, owner_a = await _register(db, "orga")
    _, owner_b = await _register(db, "orgb")
    await _add_collection(db, owner_b.tenant_id, name="shared")
    svc = CollectionService(db)

    # Foreign tenant's collection by the same name: COLLECTION_NOT_FOUND.
    with pytest.raises(AppError) as exc:
        await svc.check_access(owner_a, Permission.COLLECTION_READ, name="shared")
    assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND
    with pytest.raises(AppError) as exc:
        await svc.grant_permission(
            owner_a, name="shared", user_id=owner_a.user_id or "", role="viewer"
        )
    assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND
    # Missing name: identical result — no existence oracle.
    with pytest.raises(AppError) as exc:
        await svc.check_access(owner_a, Permission.COLLECTION_READ, name="nope")
    assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND


async def test_grant_to_non_member_rejected(db: AsyncSession) -> None:
    _, owner_a = await _register(db, "orga")
    _, owner_b = await _register(db, "orgb")
    collection_a = await _add_collection(db, owner_a.tenant_id)
    # The grantee must be a member of the collection's tenant.
    with pytest.raises(AppError) as exc:
        await CollectionService(db).grant_permission(
            owner_a, name=collection_a.name, user_id=owner_b.user_id or "", role="viewer"
        )
    assert exc.value.code == ErrorCode.TENANT_MEMBER_NOT_FOUND


async def test_grant_requires_manage(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    editor = Principal(user_id=owner.user_id, tenant_id=owner.tenant_id, role="editor")
    with pytest.raises(AppError) as exc:
        await CollectionService(db).grant_permission(
            editor, name=collection.name, user_id=owner.user_id or "", role="viewer"
        )
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE


async def test_admin_cannot_mint_owner(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    admin = Principal(user_id=owner.user_id, tenant_id=owner.tenant_id, role="admin")
    svc = CollectionService(db)
    with pytest.raises(AppError) as exc:
        await svc.grant_permission(
            admin, name=collection.name, user_id=admin.user_id or "", role="owner"
        )
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE
    # Granting at or below the granter's own rank is fine.
    grant = await svc.grant_permission(
        admin, name=collection.name, user_id=admin.user_id or "", role="admin"
    )
    assert grant.permission == "admin"


async def test_owner_can_grant_owner(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    grant = await CollectionService(db).grant_permission(
        owner, name=collection.name, user_id=owner.user_id or "", role="owner"
    )
    assert grant.permission == "owner"


async def test_grant_is_audited(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    await CollectionService(db).grant_permission(
        owner, name=collection.name, user_id=owner.user_id or "", role="editor"
    )
    row = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "collection.permission.granted",
            AuditLog.resource_id == collection.id,
        )
    )
    assert row is not None
    assert row.actor_id == owner.user_id
    assert row.details == {"user_id": owner.user_id, "role": "editor"}


async def test_revoke_permission_removes_grant(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    _, viewer = await _add_viewer_member(db, owner)
    collection = await _add_collection(db, owner.tenant_id)
    svc = CollectionService(db)
    await svc.grant_permission(
        owner, name=collection.name, user_id=viewer.user_id or "", role="editor"
    )
    assert await svc.check_access(viewer, Permission.COLLECTION_WRITE, name=collection.name)

    await svc.revoke_permission(owner, name=collection.name, user_id=viewer.user_id or "")

    # Grant gone: write checks fail again, read still passes (tenant role).
    with pytest.raises(AppError) as exc:
        await svc.check_access(viewer, Permission.COLLECTION_WRITE, name=collection.name)
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE
    assert await svc.check_access(viewer, Permission.COLLECTION_READ, name=collection.name)

    # Revocation is audited.
    row = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "collection.permission.revoked",
            AuditLog.resource_id == collection.id,
        )
    )
    assert row is not None
    assert row.details == {"user_id": viewer.user_id}


async def test_revoke_permission_is_idempotent(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    svc = CollectionService(db)
    # Revoking a grant that never existed (or was already revoked) is a no-op.
    await svc.revoke_permission(owner, name=collection.name, user_id=owner.user_id or "")
    await svc.revoke_permission(owner, name=collection.name, user_id=owner.user_id or "")


async def test_revoke_permission_foreign_collection(db: AsyncSession) -> None:
    _, owner_a = await _register(db, "orga")
    _, owner_b = await _register(db, "orgb")
    await _add_collection(db, owner_b.tenant_id, name="shared")
    with pytest.raises(AppError) as exc:
        await CollectionService(db).revoke_permission(
            owner_a, name="shared", user_id=owner_a.user_id or ""
        )
    assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND


async def test_revoke_permission_requires_manage(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    editor = Principal(user_id=owner.user_id, tenant_id=owner.tenant_id, role="editor")
    with pytest.raises(AppError) as exc:
        await CollectionService(db).revoke_permission(
            editor, name=collection.name, user_id=owner.user_id or ""
        )
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE
