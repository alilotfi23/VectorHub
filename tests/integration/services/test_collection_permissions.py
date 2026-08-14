import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import Page
from app.core.rbac import Permission, role_rank
from app.core.security import Principal
from app.db.models import AuditLog, Collection, CollectionPermission, User
from app.services.api_key_service import ApiKeyService
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


async def _key_principal(db: AsyncSession, owner: Principal, role: str = "owner") -> Principal:
    """Mint an API key for the tenant and derive its Principal exactly as
    authentication does: user_id is None and api_key_id is set — a
    key-derived principal carries tenant identity and role only, never a
    resource-level grant."""
    _, plaintext = await ApiKeyService(db).create_key(owner, name="layer2-key", role=role)
    principal = await ApiKeyService(db).authenticate(plaintext)
    assert principal is not None
    assert principal.user_id is None
    assert principal.api_key_id is not None
    return principal


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


async def test_list_permissions_returns_grants(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    _, viewer = await _add_viewer_member(db, owner)
    collection = await _add_collection(db, owner.tenant_id)
    svc = CollectionService(db)
    await svc.grant_permission(
        owner, name=collection.name, user_id=viewer.user_id or "", role="editor"
    )
    await svc.grant_permission(
        owner, name=collection.name, user_id=owner.user_id or "", role="owner"
    )

    page = await svc.list_permissions(owner, name=collection.name)
    assert len(page.items) == 2
    assert page.total == 2
    assert page.next_cursor is None  # both grants fit on one page
    by_user = {g.user_id: g.permission for g in page.items}
    assert by_user == {viewer.user_id: "editor", owner.user_id: "owner"}


async def test_list_permissions_empty(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    page = await CollectionService(db).list_permissions(owner, name=collection.name)
    assert page.items == []
    assert page.total == 0
    assert page.next_cursor is None


async def test_list_permissions_foreign_collection(db: AsyncSession) -> None:
    _, owner_a = await _register(db, "orga")
    _, owner_b = await _register(db, "orgb")
    await _add_collection(db, owner_b.tenant_id, name="shared")
    with pytest.raises(AppError) as exc:
        await CollectionService(db).list_permissions(owner_a, name="shared")
    assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND
    # Missing name: identical result — no existence oracle.
    with pytest.raises(AppError) as exc:
        await CollectionService(db).list_permissions(owner_a, name="nope")
    assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND


async def test_list_permissions_requires_manage(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    editor = Principal(user_id=owner.user_id, tenant_id=owner.tenant_id, role="editor")
    with pytest.raises(AppError) as exc:
        await CollectionService(db).list_permissions(editor, name=collection.name)
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE

    # A grant elevates: a viewer granted admin ON the collection can now list
    # its grants (editor grants don't confer TENANT_MANAGE).
    await CollectionService(db).grant_permission(
        owner, name=collection.name, user_id=owner.user_id or "", role="admin"
    )
    elevated = Principal(user_id=owner.user_id, tenant_id=owner.tenant_id, role="viewer")
    page = await CollectionService(db).list_permissions(elevated, name=collection.name)
    assert len(page.items) == 1


async def test_list_permissions_ordered_by_role_then_user_id(db: AsyncSession) -> None:
    """Deterministic ordering regardless of created_at ties: role rank desc
    (owners first), then user_id."""
    _, owner = await _register(db, "org")
    _, viewer_a = await _add_viewer_member(db, owner)
    _, viewer_b = await _add_viewer_member(db, owner)
    collection = await _add_collection(db, owner.tenant_id)

    # Insert directly with created_at values whose *time* order contradicts
    # the rank order (viewers created after the owner): only a rank-first
    # sort key produces the expected result.
    db.add_all(
        [
            CollectionPermission(
                collection_id=collection.id,
                user_id=owner.user_id or "",
                permission="owner",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            CollectionPermission(
                collection_id=collection.id,
                user_id=viewer_a.user_id or "",
                permission="viewer",
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
            CollectionPermission(
                collection_id=collection.id,
                user_id=viewer_b.user_id or "",
                permission="viewer",
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ]
    )
    await db.commit()

    page = await CollectionService(db).list_permissions(owner, name=collection.name)
    assert [(g.permission, g.user_id) for g in page.items] == [
        ("owner", owner.user_id or ""),
        ("viewer", min(viewer_a.user_id or "", viewer_b.user_id or "")),
        ("viewer", max(viewer_a.user_id or "", viewer_b.user_id or "")),
    ]


async def test_service_methods_reuse_pre_resolved_access(db: AsyncSession) -> None:
    """The route path hands a CollectionAccess into the service methods; each
    behaves identically to the name-based path (same gates apply)."""
    _, owner = await _register(db, "org")
    _, viewer = await _add_viewer_member(db, owner)
    collection = await _add_collection(db, owner.tenant_id)
    svc = CollectionService(db)

    access = await svc.check_access(owner, Permission.TENANT_MANAGE, name=collection.name)
    assert access.collection.id == collection.id

    await svc.grant_permission(owner, access=access, user_id=viewer.user_id or "", role="editor")
    assert await svc.check_access(viewer, Permission.COLLECTION_WRITE, name=collection.name)

    page = await svc.list_permissions(owner, access=access)
    assert [g.user_id for g in page.items] == [viewer.user_id]

    await svc.revoke_permission(owner, access=access, user_id=viewer.user_id or "")
    page = await svc.list_permissions(owner, access=access)
    assert page.items == []

    # Gates still fire on the access path: an editor without manage is rejected.
    editor = Principal(user_id=owner.user_id, tenant_id=owner.tenant_id, role="editor")
    with pytest.raises(AppError) as exc:
        await svc.grant_permission(
            editor, access=access, user_id=owner.user_id or "", role="viewer"
        )
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE


async def test_service_methods_require_exactly_one_of_name_or_access(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    svc = CollectionService(db)
    access = await svc.resolve_access(owner, name=collection.name)

    # Neither: programming error, not a silent 404.
    with pytest.raises(ValueError):
        await svc.grant_permission(owner, user_id=owner.user_id or "", role="viewer")
    # Both: ambiguous, rejected.
    with pytest.raises(ValueError):
        await svc.grant_permission(
            owner,
            name=collection.name,
            access=access,
            user_id=owner.user_id or "",
            role="viewer",
        )
    # Exactly one works.
    await svc.grant_permission(owner, access=access, user_id=owner.user_id or "", role="viewer")


async def test_grant_flow_resolves_collection_once(db: AsyncSession) -> None:
    """Route flow: the dependency resolves the collection once, and the
    service call that follows reuses it — no second collection lookup."""
    _, owner = await _register(db, "org")
    _, viewer = await _add_viewer_member(db, owner)
    collection = await _add_collection(db, owner.tenant_id)
    svc = CollectionService(db)

    collection_selects: list[str] = []

    def _count(
        conn: object,
        cursor: object,
        statement: object,
        params: object,
        context: object,
        executemany: bool,
    ) -> None:  # noqa: ANN001
        sql = str(statement).strip().lower()
        # `collections ` (with trailing space) matches only the collections
        # table, not collection_permissions.
        if sql.startswith("select") and "from collections " in sql:
            collection_selects.append(sql)

    bind = db.get_bind()
    engine = getattr(bind, "sync_engine", bind)
    event.listen(engine, "before_cursor_execute", _count)
    try:
        # The dependency's resolution: exactly one collections SELECT.
        access = await svc.check_access(owner, Permission.TENANT_MANAGE, name=collection.name)
        assert len(collection_selects) == 1

        # The handler's service calls reuse that access: still one SELECT.
        await svc.grant_permission(
            owner, access=access, user_id=viewer.user_id or "", role="editor"
        )
        await svc.list_permissions(owner, access=access)
        await svc.revoke_permission(owner, access=access, user_id=viewer.user_id or "")
        assert len(collection_selects) == 1
    finally:
        event.remove(engine, "before_cursor_execute", _count)


async def test_list_permissions_paginates_with_cursor(db: AsyncSession) -> None:
    """Walking the cursor returns every grant exactly once, in global
    deterministic order, with a stable total; the cursor resumes correctly
    even when the page size changes."""
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    svc = CollectionService(db)
    tenant_svc = TenantService(db)

    member_ids: dict[str, str] = {}
    for tag in ("o", "e1", "e2", "v1", "v2", "v3"):
        member = await tenant_svc.add_member(
            owner,
            tenant_id=owner.tenant_id,
            email=_unique(f"{tag}@example.com"),
            password="password-123",
            role="viewer",
        )
        member_ids[tag] = member.id
    for tag, role in (
        ("o", "owner"),
        ("e1", "editor"),
        ("e2", "editor"),
        ("v1", "viewer"),
        ("v2", "viewer"),
        ("v3", "viewer"),
    ):
        await svc.grant_permission(owner, name=collection.name, user_id=member_ids[tag], role=role)

    # Walk with limit=2: 3 pages, each <= 2 items, total stable at 6.
    pages: list[Page[CollectionPermission]] = []
    cursor: str | None = None
    for _ in range(5):
        page = await svc.list_permissions(owner, name=collection.name, limit=2, cursor=cursor)
        pages.append(page)
        assert page.total == 6
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert [g.permission for g in pages[0].items] == ["owner", "editor"]
    assert [g.permission for g in pages[1].items] == ["editor", "viewer"]
    assert [g.permission for g in pages[2].items] == ["viewer", "viewer"]
    assert len(pages) == 3
    all_ids = [g.user_id for page in pages for g in page.items]
    assert len(all_ids) == 6 and len(set(all_ids)) == 6  # no overlap, no gap
    # Concatenation preserves the global deterministic order.
    seq = [(role_rank(g.permission), g.user_id) for page in pages for g in page.items]
    assert seq == sorted(seq, key=lambda t: (-t[0], t[1]))

    # Resuming with a different page size continues exactly from the cursor.
    p = await svc.list_permissions(owner, name=collection.name, limit=3)
    assert len(p.items) == 3 and p.next_cursor is not None
    p2 = await svc.list_permissions(owner, name=collection.name, limit=2, cursor=p.next_cursor)
    assert [g.permission for g in p2.items] == ["viewer", "viewer"]
    p3 = await svc.list_permissions(owner, name=collection.name, limit=2, cursor=p2.next_cursor)
    assert [g.permission for g in p3.items] == ["viewer"]
    assert p3.next_cursor is None


async def test_list_permissions_rejects_malformed_cursor(db: AsyncSession) -> None:
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    with pytest.raises(AppError) as exc:
        await CollectionService(db).list_permissions(
            owner, name=collection.name, cursor="not-a-cursor"
        )
    assert exc.value.code == ErrorCode.VALIDATION_INVALID_CURSOR
    assert exc.value.status_code == 422


async def test_key_principal_flow_resolves_collection_once(db: AsyncSession) -> None:
    """The resolve-once contract holds for key-derived principals: a key
    principal resolves the collection once (via check_access) and the grant
    service calls reuse that access — no second collections lookup."""
    _, owner = await _register(db, "org")
    _, viewer = await _add_viewer_member(db, owner)
    collection = await _add_collection(db, owner.tenant_id)
    svc = CollectionService(db)
    key_principal = await _key_principal(db, owner)

    collection_selects: list[str] = []

    def _count(
        conn: object,
        cursor: object,
        statement: object,
        params: object,
        context: object,
        executemany: bool,
    ) -> None:  # noqa: ANN001
        sql = str(statement).strip().lower()
        if sql.startswith("select") and "from collections " in sql:
            collection_selects.append(sql)

    bind = db.get_bind()
    engine = getattr(bind, "sync_engine", bind)
    event.listen(engine, "before_cursor_execute", _count)
    try:
        # The dependency's resolution: exactly one collections SELECT.
        access = await svc.check_access(
            key_principal, Permission.TENANT_MANAGE, name=collection.name
        )
        assert len(collection_selects) == 1

        # The handler's service calls reuse that access: still one SELECT.
        await svc.grant_permission(
            key_principal, access=access, user_id=viewer.user_id or "", role="editor"
        )
        await svc.list_permissions(key_principal, access=access)
        await svc.revoke_permission(key_principal, access=access, user_id=viewer.user_id or "")
        assert len(collection_selects) == 1
    finally:
        event.remove(engine, "before_cursor_execute", _count)


async def test_key_principal_has_no_resource_grant(db: AsyncSession) -> None:
    """Keys carry tenant-level roles only: resolve_access always yields
    actor_grant=None for a key principal (grant lookups match on user_id,
    which keys don't have), so the tenant role is the ceiling."""
    _, owner = await _register(db, "org")
    collection = await _add_collection(db, owner.tenant_id)
    svc = CollectionService(db)
    owner_key = await _key_principal(db, owner)
    editor_key = await _key_principal(db, owner, role="editor")

    access = await svc.resolve_access(owner_key, name=collection.name)
    assert access.actor_grant is None

    # Owner-rank key: the tenant role alone passes the manage gate.
    assert await svc.check_access(owner_key, Permission.TENANT_MANAGE, name=collection.name)
    # Editor-rank key: no grant to elevate it, so manage is denied.
    with pytest.raises(AppError) as exc:
        await svc.check_access(editor_key, Permission.TENANT_MANAGE, name=collection.name)
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE
    assert exc.value.status_code == 403
