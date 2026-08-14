"""Layer 2 of the cross-tenant isolation suite — collection permissions.

Isolation is a security boundary, so the resource-level grant surface gets
the same discipline as the (Phase 3) vector paths: two tenants seeded with
*indistinguishable* data (identical collection names), fail-closed
assertions (error or empty, never cross-tenant rows), and a negative control
proving responses can't act as an existence oracle.

There is no adapter call in this surface (grants live in Postgres only), so
the Layer-2 routing assertions adapt directly: resolution must target the
principal's own collection row even under a same-name collision, every
cross-tenant op must fail closed *without writing*, and a forged grantee
(user_id of another tenant's user) must be rejected with no row created.
See docs/superpowers/specs/2026-08-14-tenant-isolation-tests-design.md.
"""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.security import Principal
from app.db.models import Collection, CollectionPermission, User
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


async def _add_collection(db: AsyncSession, tenant_id: str, name: str) -> Collection:
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


async def _grant_count(db: AsyncSession, collection_id: str) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(CollectionPermission)
        .where(CollectionPermission.collection_id == collection_id)
    )
    assert count is not None
    return count


async def test_same_name_collision_grants_are_isolated(db: AsyncSession) -> None:
    """Both tenants have `products`; each sees only its own grants."""
    _, owner_a = await _register(db, "orga")
    _, owner_b = await _register(db, "orgb")
    coll_a = await _add_collection(db, owner_a.tenant_id, name="products")
    coll_b = await _add_collection(db, owner_b.tenant_id, name="products")
    _, viewer_a = await _add_viewer_member(db, owner_a)
    svc = CollectionService(db)

    await svc.grant_permission(
        owner_a, name="products", user_id=viewer_a.user_id or "", role="editor"
    )

    # Same name, different physical rows.
    assert coll_a.id != coll_b.id
    assert (await svc.get_collection(owner_b, name="products")).id == coll_b.id

    # A sees its grant; B's identical name sees nothing.
    a_grants = await svc.list_permissions(owner_a, name="products")
    assert [g.user_id for g in a_grants] == [viewer_a.user_id]
    assert await svc.list_permissions(owner_b, name="products") == []

    # B revoking A's grantee by user_id lands on B's collection id — no such
    # grant exists there, so it's an idempotent no-op and A's grant survives.
    await svc.revoke_permission(owner_b, name="products", user_id=viewer_a.user_id or "")
    assert await _grant_count(db, coll_a.id) == 1
    a_grants = await svc.list_permissions(owner_a, name="products")
    assert [g.user_id for g in a_grants] == [viewer_a.user_id]

    # A's grant never leaks into B's resolution either way.
    assert await _grant_count(db, coll_b.id) == 0


async def test_cross_tenant_ops_fail_closed_without_writes(db: AsyncSession) -> None:
    _, owner_a = await _register(db, "orga")
    _, owner_b = await _register(db, "orgb")
    coll_b = await _add_collection(db, owner_b.tenant_id, name="products")
    svc = CollectionService(db)

    # Grant, list, revoke from A against B's collection: all fail closed.
    with pytest.raises(AppError) as exc:
        await svc.grant_permission(
            owner_a, name="products", user_id=owner_a.user_id or "", role="viewer"
        )
    assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND
    with pytest.raises(AppError) as exc:
        await svc.list_permissions(owner_a, name="products")
    assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND
    with pytest.raises(AppError) as exc:
        await svc.revoke_permission(owner_a, name="products", user_id=owner_a.user_id or "")
    assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND

    # Fail-closed means no partial writes: B's grants untouched, and no
    # stray rows created under B's collection by A's attempts.
    assert await svc.list_permissions(owner_b, name="products") == []
    assert await _grant_count(db, coll_b.id) == 0


async def test_cross_tenant_grantee_rejected_without_rows(db: AsyncSession) -> None:
    """Forging the grantee: A grants to B's user — rejected, zero rows."""
    _, owner_a = await _register(db, "orga")
    _, owner_b = await _register(db, "orgb")
    coll_a = await _add_collection(db, owner_a.tenant_id, name="products")

    with pytest.raises(AppError) as exc:
        await CollectionService(db).grant_permission(
            owner_a, name="products", user_id=owner_b.user_id or "", role="editor"
        )
    assert exc.value.code == ErrorCode.TENANT_MEMBER_NOT_FOUND

    # The rejection must not have created a grant row for the foreign user.
    assert await _grant_count(db, coll_a.id) == 0
    rows = await db.scalars(
        select(CollectionPermission).where(CollectionPermission.user_id == owner_b.user_id)
    )
    assert list(rows) == []


async def test_negative_control_foreign_vs_missing_identical(db: AsyncSession) -> None:
    """A probing B's collection resolves identically to a name that exists
    nowhere — no existence oracle at the service layer."""
    _, owner_a = await _register(db, "orga")
    _, owner_b = await _register(db, "orgb")
    await _add_collection(db, owner_b.tenant_id, name="products")
    svc = CollectionService(db)

    with pytest.raises(AppError) as foreign:
        await svc.list_permissions(owner_a, name="products")
    with pytest.raises(AppError) as missing:
        await svc.list_permissions(owner_a, name="does-not-exist")

    assert foreign.value.code == ErrorCode.COLLECTION_NOT_FOUND
    assert missing.value.code == ErrorCode.COLLECTION_NOT_FOUND
    assert foreign.value.message == missing.value.message
    assert foreign.value.status_code == missing.value.status_code == 404
