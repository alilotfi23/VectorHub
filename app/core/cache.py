"""Redis-backed auth caches (Phase 6 pull-forward).

Two per-request DB hits are fronted by Redis, with Postgres remaining the
source of truth and the cache failing soft (never a gate):

- **API-key principal resolution** — `vhk:principal:<key-hash>` stores the
  JSON Principal, populated on a successful authenticate and *invalidated on
  revoke*. The TTL is bounded by the key's own expiry, so a cached principal
  can never outlive a key's expiration (which would be a security bug).
- **Access-token jti deny-list** — `vhk:revoked:jti:<jti>` stores a positive
  marker only, written write-through at logout and read-through when the
  Postgres check finds a row, with TTL equal to the access-token TTL. A token
  is revoked *until the marker expires* — nothing is ever cached as "not
  revoked", so a revocation is never missed.

The client is a process singleton (one connection pool for the app's
lifetime, mirroring the adapter singletons). `get_redis()` returns None when
Redis is unconfigured, and every operation swallows connection errors and
degrades to the Postgres path — the cache is an optimization, not a gate.

The singleton is keyed to the *configured URL*: if the URL changes (env
re-read, a test fixture swapping in a container), the client is rebuilt
instead of silently kept pointing at a now-dead endpoint.
"""

import json
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.security import Principal

PRINCIPAL_CACHE_PREFIX = "vhk:principal:"
JTI_REVOKED_PREFIX = "vhk:revoked:jti:"

_client: Redis | None = None
_client_url: str | None = None


def get_redis() -> Redis | None:
    """Process-singleton async Redis client; None when unconfigured.

    Lazily built on first use (an unset/empty REDIS_URL means the cache is
    disabled and no client is ever created), so FastAPI's ASGITransport-based
    tests need no lifespan wiring. The singleton is re-keyed to the settings
    URL on change: a stale client is dropped (its pool is closed on GC; a
    clean teardown goes through close_redis()) and a fresh one is built — so
    configuration changes can't silently strand the cache at a dead endpoint.
    """
    global _client, _client_url
    url = get_settings().redis_url
    if not url:
        return None
    if _client is not None and _client_url == url:
        return _client
    _client = Redis.from_url(url, decode_responses=True)
    _client_url = url
    return _client


async def close_redis() -> None:
    """Close the singleton client (app shutdown / test teardown)."""
    global _client, _client_url
    if _client is not None:
        await _client.aclose()
        _client = None
        _client_url = None


# --- API-key principal cache ---


def _principal_cache_key(key_hash: str) -> str:
    return f"{PRINCIPAL_CACHE_PREFIX}{key_hash}"


def _principal_to_payload(principal: Principal) -> str:
    return json.dumps(
        {
            "tenant_id": principal.tenant_id,
            "role": principal.role,
            "user_id": principal.user_id,
            "is_platform_admin": principal.is_platform_admin,
            "api_key_id": principal.api_key_id,
        }
    )


def _principal_from_payload(payload: str) -> Principal:
    data = json.loads(payload)
    return Principal(
        tenant_id=data["tenant_id"],
        role=data["role"],
        user_id=data.get("user_id"),
        is_platform_admin=data.get("is_platform_admin", False),
        api_key_id=data.get("api_key_id"),
    )


async def get_cached_api_key_principal(client: Redis, key_hash: str) -> Principal | None:
    """Resolve a cached API-key principal, or None on miss/error (caller
    falls back to Postgres)."""
    try:
        payload = await client.get(_principal_cache_key(key_hash))
    except Exception:
        return None
    if payload is None:
        return None
    # With decode_responses=True the value is str; guard the bytes case
    # (e.g. a non-decoding client) rather than let mypy down the union.
    if isinstance(payload, bytes):
        payload = payload.decode()
    return _principal_from_payload(payload)


async def cache_api_key_principal(
    client: Redis, key_hash: str, principal: Principal, *, expires_at: datetime | None
) -> None:
    """Cache a resolved principal. TTL is bounded by the key's own expiry so
    the entry can never outlive `expires_at`; an already-expired key is not
    cached at all."""
    ttl = get_settings().auth_cache_ttl_seconds
    if expires_at is not None:
        remaining = int((expires_at - datetime.now(UTC)).total_seconds())
        if remaining <= 0:
            return
        ttl = min(ttl, remaining)
    try:
        await client.set(_principal_cache_key(key_hash), _principal_to_payload(principal), ex=ttl)
    except Exception:
        pass


async def invalidate_api_key_principal(client: Redis, key_hash: str) -> None:
    """Drop a key's cached principal the moment it is revoked — the
    invalidation that makes revocation take effect immediately on the
    cached path."""
    try:
        await client.delete(_principal_cache_key(key_hash))
    except Exception:
        pass


# --- Access-token jti deny-list cache ---


def _jti_key(jti: str) -> str:
    return f"{JTI_REVOKED_PREFIX}{jti}"


async def mark_jti_revoked(client: Redis, jti: str) -> None:
    """Write-through: record a revoked jti for as long as the token could
    still be presented (its TTL)."""
    ttl = get_settings().jwt_access_ttl_minutes * 60
    try:
        await client.set(_jti_key(jti), "1", ex=ttl)
    except Exception:
        pass


async def is_jti_revoked(client: Redis, jti: str) -> bool:
    """True when the jti is deny-listed; False on miss or error (caller
    falls back to Postgres). Positive markers only — a miss is never cached
    as a negative, so a revocation can't be silently skipped."""
    try:
        return bool(await client.exists(_jti_key(jti)))
    except Exception:
        return False
