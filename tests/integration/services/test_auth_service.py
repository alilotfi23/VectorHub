import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, ErrorCode
from app.core.security import Principal, decode_access_token, hash_refresh_token
from app.db.models import AuditLog, RefreshToken, RevokedToken, Tenant, User
from app.services.api_key_service import ApiKeyService
from app.services.auth_service import AuthService


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def test_register_creates_tenant_owner_and_tokens(db: AsyncSession) -> None:
    email = _unique("user@example.com")
    user, pair = await AuthService(db).register(
        email=email, password="password-123", tenant_name=_unique("acme")
    )
    assert user.role == "owner"
    assert user.is_platform_admin is False
    assert pair.token_type == "bearer"
    assert pair.expires_in == 15 * 60

    principal = decode_access_token(pair.access_token).principal
    assert principal.user_id == user.id
    assert principal.tenant_id == user.tenant_id
    assert principal.role == "owner"

    tenant = await db.get(Tenant, user.tenant_id)
    assert tenant is not None
    stored = await db.scalar(select(User).where(User.email == email))
    assert stored is not None
    assert stored.password_hash != "password-123"  # hashed at rest


async def test_register_duplicate_email(db: AsyncSession) -> None:
    email = _unique("dup@example.com")
    svc = AuthService(db)
    await svc.register(email=email, password="password-123", tenant_name=_unique("acme"))
    with pytest.raises(AppError) as exc:
        await svc.register(email=email, password="other-pass-456", tenant_name=_unique("other"))
    assert exc.value.code == ErrorCode.AUTH_EMAIL_TAKEN
    assert exc.value.status_code == 409


async def test_register_duplicate_tenant_name(db: AsyncSession) -> None:
    name = _unique("shared-name")
    svc = AuthService(db)
    await svc.register(email=_unique("a@example.com"), password="password-123", tenant_name=name)
    with pytest.raises(AppError) as exc:
        await svc.register(
            email=_unique("b@example.com"), password="password-123", tenant_name=name
        )
    assert exc.value.code == ErrorCode.TENANT_ALREADY_EXISTS


async def test_register_bootstrap_platform_admin(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = _unique("admin@example.com")
    monkeypatch.setenv("BOOTSTRAP_PLATFORM_ADMIN_EMAILS", email)
    get_settings.cache_clear()
    try:
        user, _ = await AuthService(db).register(
            email=email, password="password-123", tenant_name=_unique("acme")
        )
    finally:
        get_settings.cache_clear()
    assert user.is_platform_admin is True


async def test_login_success_and_failure(db: AsyncSession) -> None:
    email = _unique("login@example.com")
    svc = AuthService(db)
    await svc.register(email=email, password="password-123", tenant_name=_unique("acme"))

    user, pair = await svc.login(email=email, password="password-123")
    assert user.email == email
    assert pair.access_token

    with pytest.raises(AppError) as exc:
        await svc.login(email=email, password="wrong-password")
    assert exc.value.code == ErrorCode.AUTH_INVALID_CREDENTIALS
    assert exc.value.status_code == 401

    # Unknown email gets the identical error: no user enumeration.
    with pytest.raises(AppError) as exc:
        await svc.login(email=_unique("nobody@example.com"), password="password-123")
    assert exc.value.code == ErrorCode.AUTH_INVALID_CREDENTIALS


async def test_refresh_rotates_and_rejects_replay(db: AsyncSession) -> None:
    email = _unique("refresh@example.com")
    _, pair = await AuthService(db).register(
        email=email, password="password-123", tenant_name=_unique("acme")
    )

    new_pair = await AuthService(db).refresh(pair.refresh_token)
    assert new_pair.access_token != pair.access_token
    assert new_pair.refresh_token != pair.refresh_token

    # Replay of the rotated token is indistinguishable from revocation.
    with pytest.raises(AppError) as exc:
        await AuthService(db).refresh(pair.refresh_token)
    assert exc.value.code == ErrorCode.AUTH_TOKEN_REVOKED

    # The new token still works (rotation chain is live).
    refreshed = await AuthService(db).refresh(new_pair.refresh_token)
    assert refreshed.access_token


async def test_refresh_unknown_token_rejected(db: AsyncSession) -> None:
    with pytest.raises(AppError) as exc:
        await AuthService(db).refresh("never-issued-token")
    assert exc.value.code == ErrorCode.AUTH_TOKEN_REVOKED


async def test_refresh_expired_token_rejected(db: AsyncSession) -> None:
    email = _unique("expired@example.com")
    user, _ = await AuthService(db).register(
        email=email, password="password-123", tenant_name=_unique("acme")
    )
    raw = "expired-raw-token"
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(raw),
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await db.commit()

    with pytest.raises(AppError) as exc:
        await AuthService(db).refresh(raw)
    assert exc.value.code == ErrorCode.AUTH_TOKEN_EXPIRED


async def test_logout_revokes_refresh(db: AsyncSession) -> None:
    email = _unique("logout@example.com")
    _, pair = await AuthService(db).register(
        email=email, password="password-123", tenant_name=_unique("acme")
    )
    await AuthService(db).logout(pair.refresh_token)
    with pytest.raises(AppError) as exc:
        await AuthService(db).refresh(pair.refresh_token)
    assert exc.value.code == ErrorCode.AUTH_TOKEN_REVOKED
    # Idempotent: logging out twice is a no-op, not an error.
    await AuthService(db).logout(pair.refresh_token)


async def test_logout_deny_lists_access_token_jti(db: AsyncSession) -> None:
    """Logout with a bearer access token records its jti on the deny-list
    (with actor attribution) so the auth boundary can reject it immediately.
    """
    email = _unique("logout@example.com")
    user, pair = await AuthService(db).register(
        email=email, password="password-123", tenant_name=_unique("acme")
    )
    decoded = decode_access_token(pair.access_token)
    await AuthService(db).logout(
        pair.refresh_token,
        access_jti=decoded.jti,
        actor=decoded.principal,
    )

    row = await db.scalar(select(RevokedToken).where(RevokedToken.jti == decoded.jti))
    assert row is not None
    assert row.user_id == user.id
    assert row.revoked_at is not None
    # The refresh side is revoked too (existing contract).
    with pytest.raises(AppError) as exc:
        await AuthService(db).refresh(pair.refresh_token)
    assert exc.value.code == ErrorCode.AUTH_TOKEN_REVOKED


async def test_logout_purges_stale_deny_list_rows(db: AsyncSession) -> None:
    """Stale deny-list rows (older than the access-token TTL) are purged
    opportunistically on logout, so the table stays bounded."""
    email = _unique("purge@example.com")
    user, pair = await AuthService(db).register(
        email=email, password="password-123", tenant_name=_unique("acme")
    )
    stale_jti = "stale-jti"
    db.add(
        RevokedToken(
            jti=stale_jti,
            user_id=user.id,
            revoked_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await db.commit()

    decoded = decode_access_token(pair.access_token)
    await AuthService(db).logout(
        pair.refresh_token,
        access_jti=decoded.jti,
        actor=decoded.principal,
    )

    # The stale row is gone; the fresh one remains.
    assert await db.scalar(select(RevokedToken).where(RevokedToken.jti == stale_jti)) is None
    row = await db.scalar(select(RevokedToken).where(RevokedToken.jti == decoded.jti))
    assert row is not None


async def test_me_returns_user(db: AsyncSession) -> None:
    email = _unique("me@example.com")
    user, pair = await AuthService(db).register(
        email=email, password="password-123", tenant_name=_unique("acme")
    )
    principal = decode_access_token(pair.access_token).principal
    me = await AuthService(db).me(principal)
    assert me.id == user.id
    assert me.email == email


async def test_register_audits_tenant_creation(db: AsyncSession) -> None:
    email = _unique("audit@example.com")
    user, _ = await AuthService(db).register(
        email=email, password="password-123", tenant_name=_unique("acme")
    )
    row = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "tenant.created", AuditLog.tenant_id == user.tenant_id
        )
    )
    assert row is not None
    assert row.actor_id == user.id
    assert row.resource_id == user.tenant_id
    assert row.result == "success"


async def test_api_key_principal_me_rejected(db: AsyncSession) -> None:
    # API-key principals have no user_id; /auth/me must reject them.
    email = _unique("keyme@example.com")
    user, _ = await AuthService(db).register(
        email=email, password="password-123", tenant_name=_unique("acme")
    )
    owner = Principal(user_id=user.id, tenant_id=user.tenant_id, role="owner")
    _, plaintext = await ApiKeyService(db).create_key(owner, name="svc")
    principal = await ApiKeyService(db).authenticate(plaintext)
    assert principal is not None
    with pytest.raises(AppError) as exc:
        await AuthService(db).me(principal)
    assert exc.value.code == ErrorCode.AUTH_INVALID_CREDENTIALS
