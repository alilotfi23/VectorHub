import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_cached_api_key_principal, get_redis
from app.core.exceptions import AppError, ErrorCode
from app.core.rbac import Permission
from app.core.security import Principal, hash_api_key
from app.db.models import ApiKey, AuditLog, Collection, User
from app.services.api_key_service import ApiKeyService
from app.services.auth_service import AuthService
from app.services.collection_service import CollectionService


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _owner(db: AsyncSession) -> tuple[User, Principal]:
    user, _ = await AuthService(db).register(
        email=_unique("key-owner@example.com"),
        password="password-123",
        tenant_name=_unique("key-tenant"),
    )
    return user, Principal(user_id=user.id, tenant_id=user.tenant_id, role="owner")


async def test_create_key_returns_plaintext_once(db: AsyncSession) -> None:
    _, owner = await _owner(db)
    key, plaintext = await ApiKeyService(db).create_key(owner, name="ci-robot")
    assert plaintext.startswith("vhk_")
    assert key.prefix == plaintext[:12]
    assert key.role == "editor"  # least-privilege default

    stored = await db.get(ApiKey, key.id)
    assert stored is not None
    assert stored.key_hash == hash_api_key(plaintext)
    assert stored.key_hash != plaintext  # hashed at rest
    assert stored.revoked is False

    row = await db.scalar(
        select(AuditLog).where(AuditLog.action == "api_key.created", AuditLog.resource_id == key.id)
    )
    assert row is not None
    assert row.tenant_id == owner.tenant_id


async def test_create_key_with_role_and_expiry(db: AsyncSession) -> None:
    _, owner = await _owner(db)
    expires = datetime.now(UTC) + timedelta(days=1)
    key, _ = await ApiKeyService(db).create_key(
        owner, name="limited", role="viewer", expires_at=expires
    )
    assert key.role == "viewer"
    assert key.expires_at is not None


async def test_non_manager_cannot_manage_keys(db: AsyncSession) -> None:
    user, _ = await _owner(db)
    editor = Principal(user_id=user.id, tenant_id=user.tenant_id, role="editor")
    svc = ApiKeyService(db)
    with pytest.raises(AppError) as exc:
        await svc.create_key(editor, name="nope")
    assert exc.value.code == ErrorCode.AUTH_INSUFFICIENT_SCOPE
    assert exc.value.status_code == 403


async def test_list_keys_scoped_to_tenant(db: AsyncSession) -> None:
    _, owner = await _owner(db)
    svc = ApiKeyService(db)
    await svc.create_key(owner, name="one")
    await svc.create_key(owner, name="two")
    page = await svc.list_keys(owner)
    assert {k.name for k in page.items} == {"one", "two"}
    assert page.total == 2
    assert page.next_cursor is None
    assert all(k.tenant_id == owner.tenant_id for k in page.items)


async def test_list_keys_paginates_newest_first(db: AsyncSession) -> None:
    _, owner = await _owner(db)
    svc = ApiKeyService(db)
    created: list[ApiKey] = []
    for i in range(5):
        key, _ = await svc.create_key(owner, name=f"key-{i}")
        created.append(key)

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    for _ in range(10):
        page = await svc.list_keys(owner, limit=2, cursor=cursor)
        collected.extend(k.id for k in page.items)
        pages += 1
        assert page.total == 5
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert pages == 3  # 5 keys, limit 2 -> 2 + 2 + 1
    # Newest first; the two-pass stable sort matches SQL's
    # (created_at DESC, id ASC) even on a created_at tie.
    expected = sorted(created, key=lambda k: (k.created_at, k.id))
    expected.sort(key=lambda k: k.created_at, reverse=True)
    assert collected == [k.id for k in expected]
    assert len(set(collected)) == 5


async def test_authenticate_valid_revoked_and_expired(db: AsyncSession) -> None:
    _, owner = await _owner(db)
    svc = ApiKeyService(db)
    _, plaintext = await svc.create_key(owner, name="active")
    revoked_key, revoked_plaintext = await svc.create_key(owner, name="to-revoke")
    await svc.revoke_key(owner, key_id=revoked_key.id)
    expired_key, expired_plaintext = await svc.create_key(
        owner, name="expired", expires_at=datetime.now(UTC) - timedelta(minutes=1)
    )

    principal = await svc.authenticate(plaintext)
    assert principal is not None
    assert principal.tenant_id == owner.tenant_id
    assert principal.role == "editor"
    assert principal.api_key_id is not None
    assert principal.user_id is None

    assert await svc.authenticate(revoked_plaintext) is None
    assert await svc.authenticate(expired_plaintext) is None
    assert await svc.authenticate("vhk_bogus-key") is None


async def test_revoke_key_and_cross_tenant_isolation(db: AsyncSession) -> None:
    _, owner_a = await _owner(db)
    user_b, _ = await AuthService(db).register(
        email=_unique("other@example.com"),
        password="password-123",
        tenant_name=_unique("other-tenant"),
    )
    owner_b = Principal(user_id=user_b.id, tenant_id=user_b.tenant_id, role="owner")

    key_a, _ = await ApiKeyService(db).create_key(owner_a, name="a-key")
    # Revoking from another tenant must look like the key doesn't exist.
    with pytest.raises(AppError) as exc:
        await ApiKeyService(db).revoke_key(owner_b, key_id=key_a.id)
    assert exc.value.code == ErrorCode.API_KEY_NOT_FOUND

    await ApiKeyService(db).revoke_key(owner_a, key_id=key_a.id)
    revoked = await db.get(ApiKey, key_a.id)
    assert revoked is not None and revoked.revoked is True
    row = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "api_key.revoked", AuditLog.resource_id == key_a.id
        )
    )
    assert row is not None


async def test_key_death_is_immediate_and_a_property_of_the_key(db: AsyncSession) -> None:
    """Key death is enforced at the authentication gate the moment it
    happens, and is a property of the key itself: authenticate is a pure
    key-hash lookup with no caller context, so a revoked key resolves to
    None immediately (and stays dead on every repeated presentation),
    while a live key always resolves to its own tenant's principal — it
    can never be bent into a foreign identity. Revocation is a flag, not
    deletion: the row survives (auditable) but can't be replayed into a
    Principal."""
    _, owner = await _owner(db)
    svc = ApiKeyService(db)

    live, live_plaintext = await svc.create_key(owner, name="live")
    doomed, doomed_plaintext = await svc.create_key(owner, name="doomed")

    # Both live right now.
    assert await svc.authenticate(doomed_plaintext) is not None

    # Immediate: the very next authenticate after revoke is None.
    await svc.revoke_key(owner, key_id=doomed.id)
    assert await svc.authenticate(doomed_plaintext) is None

    # Global and stable: repeated presentation stays dead, and the survivor
    # still resolves to its own tenant — never a foreign identity.
    assert await svc.authenticate(doomed_plaintext) is None
    resolved = await svc.authenticate(live_plaintext)
    assert resolved is not None
    assert resolved.tenant_id == owner.tenant_id
    assert resolved.api_key_id == live.id

    # The revoked flag — not row removal — is what gates access, so the
    # key's lingering row can't be replayed into a Principal.
    stored = await db.get(ApiKey, doomed.id)
    assert stored is not None and stored.revoked is True
    assert await svc.authenticate(doomed_plaintext) is None


async def test_principal_cache_invalidated_on_revoke(db: AsyncSession, redis_url: str) -> None:
    """With the Redis cache engaged, a revoked key dies immediately on the
    cached path: revoke invalidates the cached principal, so the next
    authenticate is a fresh DB lookup (revoked -> None), never a cache hit."""
    _, owner = await _owner(db)
    svc = ApiKeyService(db)
    key, plaintext = await svc.create_key(owner, name="cached")
    key_hash = hash_api_key(plaintext)

    # First authenticate populates the cache.
    principal = await svc.authenticate(plaintext)
    assert principal is not None
    redis = get_redis()
    assert redis is not None
    cached = await get_cached_api_key_principal(redis, key_hash)
    assert cached is not None
    assert cached.api_key_id == key.id

    # Revoke drops the cache entry immediately...
    await svc.revoke_key(owner, key_id=key.id)
    assert await get_cached_api_key_principal(redis, key_hash) is None
    # ...so the next authenticate falls through to Postgres and sees the
    # revoked flag: no revivification window on the cached path.
    assert await svc.authenticate(plaintext) is None


async def test_revoked_key_role_unreachable_through_resolve_once_gates(db: AsyncSession) -> None:
    """The resolve-once gates trust the principal's role and never re-check
    the credential, so revocation must be — and is — enforced at the
    derivation boundary. Compositional proof: revocation flips only the
    revoked flag (the role row is untouched, so the gates would still pass
    with a stale principal), yet the principal can no longer be derived —
    the flag, not the role, is the enforcement point, and the gates have no
    window in which to consult a dead key's tenant role."""
    _, owner = await _owner(db)
    collection = Collection(
        tenant_id=owner.tenant_id,
        name="products",
        backend="chroma",
        dimension=8,
        distance_metric="cosine",
        physical_name=f"col_{uuid.uuid4().hex[:12]}",
    )
    db.add(collection)
    await db.commit()
    svc = CollectionService(db)

    key, plaintext = await ApiKeyService(db).create_key(owner, name="role-key", role="owner")

    # Live: the owner role passes the resolve-once manage gate, and keys
    # never hold resource grants — the tenant role is all there is.
    principal = await ApiKeyService(db).authenticate(plaintext)
    assert principal is not None
    access = await svc.check_access(principal, Permission.TENANT_MANAGE, name=collection.name)
    assert access.actor_grant is None

    # Revoke: only the flag flips — the role is unchanged in the DB, so a
    # stale principal would still sail through the gates.
    assert key.role == "owner"
    await ApiKeyService(db).revoke_key(owner, key_id=key.id)
    stored = await db.get(ApiKey, key.id)
    assert stored is not None
    assert stored.revoked is True and stored.role == "owner"

    # The derivation is the enforcement point: it returns None, so no stale
    # principal exists for the gates to resolve with — the timing gap is
    # closed at the only boundary it can be closed at.
    assert await ApiKeyService(db).authenticate(plaintext) is None
