"""Authentication: register, login, refresh (rotating), logout, me.

Refresh tokens are opaque and stored hashed; rotation is atomic (a
conditional UPDATE claims the row, so a replayed token is rejected as
revoked). Access tokens are short-lived stateless JWTs — their jti-based
revocation/caching lands in Phase 6 with Redis.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, ErrorCode
from app.core.security import (
    Principal,
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.db.models import RefreshToken, Tenant, User
from app.services.audit_service import AuditService


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 0  # access token TTL in seconds, set at issue time


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def register(
        self, *, email: str, password: str, tenant_name: str
    ) -> tuple[User, TokenPair]:
        settings = get_settings()
        email = email.strip().lower()
        if await self._session.scalar(select(User).where(User.email == email)):
            raise AppError(
                ErrorCode.AUTH_EMAIL_TAKEN, "Email is already registered", status_code=409
            )
        if await self._session.scalar(select(Tenant).where(Tenant.name == tenant_name)):
            raise AppError(
                ErrorCode.TENANT_ALREADY_EXISTS, "Tenant name is already taken", status_code=409
            )

        tenant = Tenant(name=tenant_name)
        self._session.add(tenant)
        await self._session.flush()  # assigns tenant.id

        is_platform_admin = email in settings.platform_admin_emails
        user = User(
            tenant_id=tenant.id,
            email=email,
            password_hash=hash_password(password),
            role="owner",
            is_platform_admin=is_platform_admin,
        )
        self._session.add(user)
        await self._session.flush()

        pair = await self._issue_tokens(user)
        await self._audit.record(
            tenant_id=tenant.id,
            actor_id=user.id,
            action="tenant.created",
            resource_type="tenant",
            resource_id=tenant.id,
        )
        try:
            await self._session.commit()
        except IntegrityError as exc:
            raise self._map_integrity_error(exc) from exc
        return user, pair

    async def login(self, *, email: str, password: str) -> tuple[User, TokenPair]:
        email = email.strip().lower()
        user = await self._session.scalar(select(User).where(User.email == email))
        # Same error for unknown email and wrong password: no user enumeration.
        if user is None or not verify_password(password, user.password_hash):
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS, "Invalid email or password", status_code=401
            )
        pair = await self._issue_tokens(user)
        await self._session.commit()
        return user, pair

    async def refresh(self, refresh_token: str) -> TokenPair:
        token_hash = hash_refresh_token(refresh_token)
        row = await self._session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        if row is None or row.revoked:
            # Unknown or already-rotated token — replay of a rotated token
            # must be indistinguishable from a revoked one.
            raise AppError(
                ErrorCode.AUTH_TOKEN_REVOKED, "Refresh token is revoked", status_code=401
            )
        if row.expires_at < datetime.now(UTC):
            raise AppError(ErrorCode.AUTH_TOKEN_EXPIRED, "Refresh token expired", status_code=401)

        # Atomic rotation: claim the row so concurrent replays lose the race.
        claimed = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.id == row.id, RefreshToken.revoked.is_(False))
            .values(revoked=True)
            .returning(RefreshToken.id)
        )
        if claimed.scalar_one_or_none() is None:
            raise AppError(
                ErrorCode.AUTH_TOKEN_REVOKED, "Refresh token is revoked", status_code=401
            )

        user = await self._session.get(User, row.user_id)
        if user is None:
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS, "User no longer exists", status_code=401
            )
        pair = await self._issue_tokens(user)
        await self._session.commit()
        return pair

    async def logout(self, refresh_token: str) -> None:
        # Idempotent: revoking an already-revoked or unknown token is a no-op.
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == hash_refresh_token(refresh_token))
            .values(revoked=True)
        )
        await self._session.commit()

    async def me(self, principal: Principal) -> User:
        user = await self._session.get(User, principal.user_id) if principal.user_id else None
        if user is None:
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS, "User no longer exists", status_code=401
            )
        return user

    async def _issue_tokens(self, user: User) -> TokenPair:
        settings = get_settings()
        access = create_access_token(user.id, user.tenant_id, user.role, user.is_platform_admin)
        raw = generate_refresh_token()
        self._session.add(
            RefreshToken(
                user_id=user.id,
                token_hash=hash_refresh_token(raw),
                expires_at=datetime.now(UTC) + timedelta(days=settings.jwt_refresh_ttl_days),
            )
        )
        await self._session.flush()
        return TokenPair(
            access_token=access,
            refresh_token=raw,
            expires_in=settings.jwt_access_ttl_minutes * 60,
        )

    @staticmethod
    def _map_integrity_error(exc: IntegrityError) -> AppError:
        # Constraint names are deterministic: users.email -> users_email_key,
        # tenants.name -> tenants_name_key (both generated by SQLAlchemy).
        detail = str(exc.orig)
        if "users_email_key" in detail:
            return AppError(
                ErrorCode.AUTH_EMAIL_TAKEN, "Email is already registered", status_code=409
            )
        if "tenants_name_key" in detail:
            return AppError(
                ErrorCode.TENANT_ALREADY_EXISTS, "Tenant name is already taken", status_code=409
            )
        return AppError(ErrorCode.AUTH_INVALID_CREDENTIALS, "Registration failed", status_code=409)
