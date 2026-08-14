"""API-key lifecycle (tenant-scoped, hashed at rest) and authentication.

Management (create/list/revoke) requires TENANT_MANAGE (admin/owner). Keys
carry a tenant-level role so programmatic principals scope below full
access. Plaintext is shown exactly once at creation; per-key rate limits
are stored but enforced in Phase 6.
"""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import (
    cache_api_key_principal,
    get_cached_api_key_principal,
    get_redis,
    invalidate_api_key_principal,
)
from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import Page, paginate
from app.core.rbac import VALID_ROLES, Permission, has_permission
from app.core.security import Principal, generate_api_key, hash_api_key
from app.db.models import ApiKey
from app.services.audit_service import AuditService


class ApiKeyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def create_key(
        self,
        actor: Principal,
        *,
        name: str,
        role: str = "editor",
        expires_at: datetime | None = None,
        rate_limit_qps: int | None = None,
    ) -> tuple[ApiKey, str]:
        self._require_manage(actor)
        if role not in VALID_ROLES:
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS, f"Invalid role: {role}", status_code=422
            )
        plaintext, prefix = generate_api_key()
        key = ApiKey(
            tenant_id=actor.tenant_id,
            name=name,
            key_hash=hash_api_key(plaintext),
            prefix=prefix,
            role=role,
            expires_at=expires_at,
            rate_limit_qps=rate_limit_qps,
        )
        self._session.add(key)
        await self._session.flush()
        await self._audit.record(
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            action="api_key.created",
            resource_type="api_key",
            resource_id=key.id,
            details={"name": name, "role": role, "prefix": prefix},
        )
        await self._session.commit()
        return key, plaintext

    async def list_keys(
        self,
        actor: Principal,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[ApiKey]:
        """List the tenant's API keys, cursor-paginated so large key sets are
        never fully materialized.

        Tenant-scoped. Keyset pagination over the deterministic sort —
        created_at descending (newest first), id ascending as the
        total-order tiebreak. The sort key is epoch-microseconds (an integer
        expression), so the opaque cursor carries only JSON scalars and the
        keyset comparison is int-vs-int with no timestamp coercion
        ambiguity.
        """
        self._require_manage(actor)
        epoch_micros = cast(func.extract("epoch", ApiKey.created_at) * 1_000_000, BigInteger)
        return await paginate(
            session=self._session,
            base=select(ApiKey).where(ApiKey.tenant_id == actor.tenant_id),
            count=select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.tenant_id == actor.tenant_id),
            sort_keys=[(epoch_micros, "desc"), (ApiKey.id, "asc")],
            limit=limit,
            cursor=cursor,
            row_key_values=lambda k: [int(k.created_at.timestamp() * 1_000_000), k.id],
        )

    async def revoke_key(self, actor: Principal, *, key_id: str) -> None:
        self._require_manage(actor)
        key = await self._session.scalar(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == actor.tenant_id)
        )
        if key is None:
            raise AppError(ErrorCode.API_KEY_NOT_FOUND, "API key not found", status_code=404)
        await self._session.execute(update(ApiKey).where(ApiKey.id == key.id).values(revoked=True))
        # Invalidation-on-revoke: the cached principal must die the moment
        # the key does, or a cache hit would resurrect a revoked key.
        redis = get_redis()
        if redis is not None:
            await invalidate_api_key_principal(redis, key.key_hash)
        await self._audit.record(
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            action="api_key.revoked",
            resource_type="api_key",
            resource_id=key.id,
        )
        await self._session.commit()

    async def authenticate(self, plaintext: str) -> Principal | None:
        """Resolve a presented key to a Principal, or None if invalid.

        Cache-first: a cached principal short-circuits the DB lookup. The
        cache is populated only for *valid* keys (never for revoked/expired
        results) and its TTL is bounded by the key's own expiry, so a hit is
        always safe; revoke invalidates explicitly.
        """
        key_hash = hash_api_key(plaintext)
        redis = get_redis()
        if redis is not None:
            cached = await get_cached_api_key_principal(redis, key_hash)
            if cached is not None:
                return cached
        key = await self._session.scalar(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked.is_(False))
        )
        if key is None:
            return None
        if key.expires_at is not None and key.expires_at < datetime.now(UTC):
            return None
        principal = Principal(
            tenant_id=key.tenant_id,
            role=key.role,
            api_key_id=key.id,
        )
        if redis is not None:
            await cache_api_key_principal(redis, key_hash, principal, expires_at=key.expires_at)
        return principal

    def _require_manage(self, actor: Principal) -> None:
        if not has_permission(actor, Permission.TENANT_MANAGE):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                "Admin or owner role required to manage API keys",
                status_code=403,
            )
