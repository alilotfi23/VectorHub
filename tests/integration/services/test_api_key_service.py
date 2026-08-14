import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.security import Principal, hash_api_key
from app.db.models import ApiKey, AuditLog, User
from app.services.api_key_service import ApiKeyService
from app.services.auth_service import AuthService


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
    keys = await svc.list_keys(owner)
    assert {k.name for k in keys} == {"one", "two"}
    assert all(k.tenant_id == owner.tenant_id for k in keys)


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
