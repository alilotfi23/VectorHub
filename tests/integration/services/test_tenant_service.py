import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import Page
from app.core.security import Principal, hash_password
from app.db.models import AuditLog, User
from app.services.api_key_service import ApiKeyService
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
    page = await svc.list_members(owner, tenant_id=owner.tenant_id)
    assert {m.role for m in page.items} == {"owner", "editor", "viewer"}
    assert page.total == 3
    assert page.next_cursor is None  # all members fit on one page


async def test_list_members_cross_tenant_hidden(db: AsyncSession) -> None:
    _, owner_a = await _register(db, "orga")
    user_b, _ = await _register(db, "orgb")
    with pytest.raises(AppError) as exc:
        await TenantService(db).list_members(owner_a, tenant_id=user_b.tenant_id)
    assert exc.value.code == ErrorCode.TENANT_NOT_FOUND


async def test_list_members_ordered_by_role_then_email(db: AsyncSession) -> None:
    """Deterministic ordering regardless of created_at ties: role rank desc
    (owners first), then email."""
    _, owner = await _register(db, "org")
    past = datetime(2026, 1, 1, tzinfo=UTC)
    # Insert viewers with identical created_at *before* the owner's (created
    # at registration, later): only a rank-first key puts the owner first,
    # and the viewers' tie is decided by email.
    db.add_all(
        [
            User(
                tenant_id=owner.tenant_id,
                email=_unique("aa-viewer@example.com"),
                password_hash=hash_password("password-123"),
                role="viewer",
                created_at=past,
            ),
            User(
                tenant_id=owner.tenant_id,
                email=_unique("bb-viewer@example.com"),
                password_hash=hash_password("password-123"),
                role="viewer",
                created_at=past,
            ),
        ]
    )
    await db.commit()

    page = await TenantService(db).list_members(owner, tenant_id=owner.tenant_id)
    assert [m.role for m in page.items] == ["owner", "viewer", "viewer"]
    assert page.items[1].email < page.items[2].email


async def test_member_ops_reuse_pre_resolved_tenant(db: AsyncSession) -> None:
    """The route path hands a resolved Tenant into the service methods; each
    behaves identically to the tenant_id-based path (same gates apply)."""
    _, owner = await _register(db, "org")
    svc = TenantService(db)

    tenant = await svc.resolve_tenant(owner, tenant_id=owner.tenant_id)
    assert tenant.id == owner.tenant_id

    member = await svc.add_member(
        owner,
        tenant=tenant,
        email=_unique("v@example.com"),
        password="password-123",
        role="viewer",
    )
    assert member.tenant_id == owner.tenant_id

    page = await svc.list_members(owner, tenant=tenant)
    assert {m.id for m in page.items} == {owner.user_id, member.id}

    updated = await svc.change_member_role(owner, tenant=tenant, user_id=member.id, role="editor")
    assert updated.role == "editor"

    # Gates still fire on the pre-resolved path: an editor cannot add members.
    editor = Principal(user_id=owner.user_id, tenant_id=owner.tenant_id, role="editor")
    with pytest.raises(AppError) as exc:
        await svc.add_member(
            editor,
            tenant=tenant,
            email=_unique("x@example.com"),
            password="password-123",
            role="viewer",
        )
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE


async def test_member_ops_require_exactly_one_of_tenant_id_or_tenant(
    db: AsyncSession,
) -> None:
    _, owner = await _register(db, "org")
    svc = TenantService(db)
    tenant = await svc.resolve_tenant(owner, tenant_id=owner.tenant_id)

    # Neither: programming error, not a silent 404.
    with pytest.raises(ValueError):
        await svc.add_member(
            owner, email=_unique("x@example.com"), password="password-123", role="viewer"
        )
    # Both: ambiguous, rejected.
    with pytest.raises(ValueError):
        await svc.add_member(
            owner,
            tenant_id=owner.tenant_id,
            tenant=tenant,
            email=_unique("y@example.com"),
            password="password-123",
            role="viewer",
        )
    # Exactly one works.
    await svc.add_member(
        owner, tenant=tenant, email=_unique("z@example.com"), password="password-123", role="viewer"
    )


async def test_member_flow_resolves_tenant_once(db: AsyncSession) -> None:
    """Route flow: the dependency resolves the tenant once, and the service
    calls that follow reuse it — no second tenant lookup."""
    _, owner = await _register(db, "org")
    svc = TenantService(db)

    tenant_selects: list[str] = []

    def _count(
        conn: object,
        cursor: object,
        statement: object,
        params: object,
        context: object,
        executemany: bool,
    ) -> None:  # noqa: ANN001
        sql = str(statement).strip().lower()
        if sql.startswith("select") and "from tenants " in sql:
            tenant_selects.append(sql)

    bind = db.get_bind()
    engine = getattr(bind, "sync_engine", bind)
    event.listen(engine, "before_cursor_execute", _count)
    try:
        # The dependency's resolution: exactly one tenants SELECT.
        tenant = await svc.resolve_tenant(owner, tenant_id=owner.tenant_id)
        assert len(tenant_selects) == 1

        # The handler's service calls reuse that tenant: still one SELECT.
        member = await svc.add_member(
            owner,
            tenant=tenant,
            email=_unique("q@example.com"),
            password="password-123",
            role="viewer",
        )
        await svc.list_members(owner, tenant=tenant)
        await svc.change_member_role(owner, tenant=tenant, user_id=member.id, role="editor")
        assert len(tenant_selects) == 1
    finally:
        event.remove(engine, "before_cursor_execute", _count)


async def test_member_flow_resolves_tenant_once_for_key_principal(db: AsyncSession) -> None:
    """The resolve-once contract holds for key-derived principals on the
    member surface too: a key principal resolves the tenant once and the
    member service calls reuse it — no second tenants lookup."""
    _, owner = await _register(db, "org")
    svc = TenantService(db)

    # Mint an API key and derive its principal exactly as authentication does.
    _, plaintext = await ApiKeyService(db).create_key(owner, name="layer2-key", role="owner")
    key_principal = await ApiKeyService(db).authenticate(plaintext)
    assert key_principal is not None
    assert key_principal.user_id is None

    tenant_selects: list[str] = []

    def _count(
        conn: object,
        cursor: object,
        statement: object,
        params: object,
        context: object,
        executemany: bool,
    ) -> None:  # noqa: ANN001
        sql = str(statement).strip().lower()
        if sql.startswith("select") and "from tenants " in sql:
            tenant_selects.append(sql)

    bind = db.get_bind()
    engine = getattr(bind, "sync_engine", bind)
    event.listen(engine, "before_cursor_execute", _count)
    try:
        # The dependency's resolution: exactly one tenants SELECT.
        tenant = await svc.resolve_tenant(key_principal, tenant_id=owner.tenant_id)
        assert len(tenant_selects) == 1

        # The handler's service calls reuse that tenant: still one SELECT.
        member = await svc.add_member(
            key_principal,
            tenant=tenant,
            email=_unique("key@example.com"),
            password="password-123",
            role="viewer",
        )
        await svc.list_members(key_principal, tenant=tenant)
        await svc.change_member_role(key_principal, tenant=tenant, user_id=member.id, role="editor")
        assert len(tenant_selects) == 1

        # Key principals audit without a user identity (actor_id is None).
        row = await db.scalar(
            select(AuditLog).where(
                AuditLog.action == "member.added", AuditLog.resource_id == member.id
            )
        )
        assert row is not None
        assert row.actor_id is None
    finally:
        event.remove(engine, "before_cursor_execute", _count)


async def test_list_members_paginates_with_cursor(db: AsyncSession) -> None:
    """Walking the cursor returns every member exactly once, rank-ordered,
    with a stable total."""
    _, owner = await _register(db, "org")
    svc = TenantService(db)
    member_ids: dict[str, str] = {}
    for tag in ("e1", "v1", "v2", "v3"):
        member = await svc.add_member(
            owner,
            tenant_id=owner.tenant_id,
            email=_unique(f"{tag}@example.com"),
            password="password-123",
            role="viewer",
        )
        member_ids[tag] = member.id
    await svc.change_member_role(
        owner, tenant_id=owner.tenant_id, user_id=member_ids["e1"], role="editor"
    )

    pages: list[Page[User]] = []
    cursor: str | None = None
    for _ in range(5):
        page = await svc.list_members(owner, tenant_id=owner.tenant_id, limit=2, cursor=cursor)
        pages.append(page)
        assert page.total == 5  # owner + editor + 3 viewers
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert [m.role for m in pages[0].items] == ["owner", "editor"]
    assert [m.role for m in pages[1].items] == ["viewer", "viewer"]
    assert [m.role for m in pages[2].items] == ["viewer"]
    assert len(pages) == 3
    all_emails = [m.email for page in pages for m in page.items]
    assert len(all_emails) == 5 and len(set(all_emails)) == 5  # no overlap, no gap


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
