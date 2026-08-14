"""Redis-backed token-bucket rate limiting (Phase 6 pull-forward).

One atomic Lua script per request keeps the bucket state correct under
concurrency (HMSET + EXPIRE in one eval). Buckets are keyed by kind:

- **route** — `vhk:rl:route:<METHOD> <path>` (literal path; dynamic path
  segments are distinct buckets — tenant/key caps are the cross-segment
  backstop). Rate from settings: per-route override or the platform default.
- **tenant** — `vhk:rl:tenant:<id>`, applied when the tenant's
  ``rate_limit_qps`` row is set (cached read-through below).
- **api_key** — `vhk:rl:key:<id>`, applied when the key's ``rate_limit_qps``
  row is set.

Tenant/key rates are resolved via a Redis config key with DB read-through:
`vhk:rl:config:<kind>:<id>` caches the rate for
``rate_limit_config_cache_ttl_seconds`` (including a negative entry, so a
row without a cap is cached as "none"). This keeps rate enforcement off the
per-request DB path in steady state while staying self-healing after a Redis
flush; a rate change takes effect within the TTL.

Every operation fails **open**: if Redis is unreachable the limiter lets the
request through rather than breaking the API — rate limiting is a mitigation,
never an availability gate. The middleware is the only caller.
"""

import asyncio
import time
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy import Executable, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import ApiKey, Tenant

RL_ROUTE_PREFIX = "vhk:rl:route:"
RL_TENANT_PREFIX = "vhk:rl:tenant:"
RL_KEY_PREFIX = "vhk:rl:key:"
RL_CONFIG_PREFIX = "vhk:rl:config:"

# Cap the DB read-through so a hung/unreachable Postgres stalls a request by
# at most this long before the limiter fails open (asyncpg's own connect
# timeout is 60s — far too long to spend on a rate-limit config lookup).
RATE_LIMIT_DB_TIMEOUT_SECONDS = 2.0

LimitKind = Literal["route_qps", "tenant_qps", "api_key_qps"]

_TOKEN_BUCKET_LUA = """
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(bucket[1])
if not tokens then tokens = capacity end
local ts = tonumber(bucket[2])
if not ts then ts = now end
tokens = math.min(capacity, tokens + (now - ts) / 1000.0 * rate)
local allowed = 0
local retry_after = 0
if tokens >= 1.0 then
  tokens = tokens - 1.0
  allowed = 1
else
  retry_after = math.ceil((1.0 - tokens) / rate)
end
redis.call('HMSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate) + 5)
return {allowed, retry_after}
"""


def _bucket_capacity(rate: float) -> float:
    """Burst allowance: `rate * multiplier` (at least 1 token)."""
    return max(1.0, rate * get_settings().rate_limit_burst_multiplier)


def _now_ms() -> int:
    return int(time.time() * 1000)


async def consume_token(redis: Redis | None, key: str, rate: float) -> tuple[bool, int]:
    """Consume one token from the bucket at `key`; returns (allowed, retry-after).

    Fails open: no Redis (or any Redis error) means the request is allowed.
    A non-positive rate also means unlimited.
    """
    if redis is None or rate <= 0:
        return True, 0
    capacity = _bucket_capacity(rate)
    try:
        # redis-py 5's stubs type eval as Union[Awaitable[Any], Any] (a
        # sync/async union); it is async here.
        result = await redis.eval(  # type: ignore[misc]
            _TOKEN_BUCKET_LUA,
            1,
            key,
            str(rate),
            str(capacity),
            str(_now_ms()),
        )
    except Exception:
        return True, 0
    return bool(result[0]), max(1, int(result[1]))


def route_rate(route_key: str) -> float:
    """QPS for a route key: per-route override, else the platform default."""
    settings = get_settings()
    return settings.rate_limit_route_qps.get(route_key, settings.rate_limit_default_qps)


async def resolve_tenant_rate(
    redis: Redis | None, session: AsyncSession, tenant_id: str
) -> float | None:
    """The tenant's configured QPS cap, cached read-through; None = no cap."""
    return await _resolve_rate(
        redis,
        session,
        "tenant",
        tenant_id,
        select(Tenant.rate_limit_qps).where(Tenant.id == tenant_id),
    )


async def resolve_api_key_rate(
    redis: Redis | None, session: AsyncSession, api_key_id: str
) -> float | None:
    """The key's configured QPS cap, cached read-through; None = no cap."""
    return await _resolve_rate(
        redis,
        session,
        "key",
        api_key_id,
        select(ApiKey.rate_limit_qps).where(ApiKey.id == api_key_id),
    )


async def _resolve_rate(
    redis: Redis | None,
    session: AsyncSession,
    kind: str,
    entity_id: str,
    query: Executable,
) -> float | None:
    """Config-key read with DB read-through; positive and negative caching."""
    cache_key = f"{RL_CONFIG_PREFIX}{kind}:{entity_id}"
    if redis is not None:
        try:
            cached = await redis.get(cache_key)
        except Exception:
            cached = None
        if cached is not None:
            # Negative entries are cached as "" so a row without a cap is
            # distinguishable from a miss.
            return None if cached == "" else float(cached)
    try:
        row = await asyncio.wait_for(session.scalar(query), timeout=RATE_LIMIT_DB_TIMEOUT_SECONDS)
    except Exception:
        return None
    rate = float(row) if row is not None else None
    if redis is not None:
        try:
            await redis.set(
                cache_key,
                "" if rate is None else str(rate),
                ex=get_settings().rate_limit_config_cache_ttl_seconds,
            )
        except Exception:
            pass
    return rate
