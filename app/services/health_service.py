"""Dependency health checks for GET /health (Phase 7 pull-forward).

The probe reports one status per dependency — "ok" or "down" — plus an
overall status per the CLAUDE.md contract:

- **postgres** (critical): connect + a trivial query via the request session.
- **redis** (critical): PING through the shared cache singleton.
- **workers**: freshness of arq worker heartbeats written to Redis under
  ``WORKER_HEARTBEAT_PREFIX``. The Phase 6 arq worker writes
  ``vhk:worker:heartbeat:<worker-id> = <unix-epoch-ts>`` (TTL > heartbeat
  interval); any heartbeat newer than ``worker_heartbeat_ttl_seconds`` means
  a live worker. No fresh heartbeat => "down" — a deployment with no worker
  reports degraded until Phase 6 ships one, which is the honest answer.
- **adapters**: one entry per backend in the AdapterRegistry, each via that
  adapter's ``health_check()`` (timeout-bounded so a hung backend can't hang
  the probe).

Critical deps (postgres, redis) drive overall "down" (HTTP 503); non-critical
failures degrade to 200 with overall "degraded" so a single backend outage
doesn't trigger k8s restart loops. Every check is wrapped in a timeout.
"""

import asyncio
import time
from collections.abc import Awaitable
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import registry
from app.core.cache import get_redis
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import health_check_outcome
from app.schemas.health import HealthReport
from app.workers.heartbeat import WORKER_HEARTBEAT_PREFIX

_CHECK_TIMEOUT_SECONDS = 5.0

CheckStatus = Literal["ok", "down"]

logger = get_logger("health")


async def check_health(session: AsyncSession) -> HealthReport:
    """Probe every dependency and aggregate per the ok/degraded/down contract.

    Postgres and Redis are critical: if either is down the overall status is
    "down" regardless of the non-critical checks. Workers and adapters are
    non-critical: any failure there degrades (200) rather than fails the probe.

    Every outcome flows through ``_probe``, which records the counter AND a
    structured ``health_probe`` line (check, status, duration_ms) so probe
    outcomes join the same log stream as the request logs. Health probes hit
    the admin app, which deliberately carries no middleware, so these lines
    carry no request_id — correlate across probes by check + timestamp.
    """
    postgres: CheckStatus = await _probe("postgres", _check_postgres(session))
    redis: CheckStatus = await _probe("redis", _check_redis())
    workers: CheckStatus = await _probe("workers", _check_workers(redis_ok=redis == "ok"))
    adapters: dict[str, str] = await _check_adapters()

    if postgres != "ok" or redis != "ok":
        status: Literal["ok", "degraded", "down"] = "down"
    elif workers != "ok" or any(v != "ok" for v in adapters.values()):
        status = "degraded"
    else:
        status = "ok"

    checks: dict[str, str | dict[str, str]] = {
        "postgres": postgres,
        "redis": redis,
        "workers": workers,
        "adapters": adapters,
    }
    return HealthReport(status=status, checks=checks)


async def _check_postgres(session: AsyncSession) -> CheckStatus:
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=_CHECK_TIMEOUT_SECONDS)
        return "ok"
    except Exception:
        return "down"


async def _check_redis() -> CheckStatus:
    redis = get_redis()
    if redis is None:
        return "down"
    try:
        await asyncio.wait_for(redis.ping(), timeout=_CHECK_TIMEOUT_SECONDS)
        return "ok"
    except Exception:
        return "down"


async def _check_workers(*, redis_ok: bool) -> CheckStatus:
    if not redis_ok:
        # Can't verify liveness without Redis; the redis check itself already
        # reports the failure, and overall status is down regardless.
        return "down"
    redis = get_redis()
    if redis is None:
        return "down"
    try:
        fresh = await asyncio.wait_for(
            _has_fresh_worker_heartbeat(redis), timeout=_CHECK_TIMEOUT_SECONDS
        )
        return "ok" if fresh else "down"
    except Exception:
        return "down"


async def _has_fresh_worker_heartbeat(redis: Redis) -> bool:
    """True when any worker heartbeat is newer than the configured TTL."""
    now = time.time()
    ttl = get_settings().worker_heartbeat_ttl_seconds
    async for key in redis.scan_iter(match=f"{WORKER_HEARTBEAT_PREFIX}*", count=100):
        raw = await redis.get(key)
        if raw is None:
            continue
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            continue
        if now - ts <= ttl:
            return True
    return False


async def _probe(name: str, coro: Awaitable[CheckStatus]) -> CheckStatus:
    """Run one check and record its outcome: a counter AND a structured log
    line with timing. Exceptions that escape the check count as "down" — the
    callers' own try/excepts are the primary guard, this is the belt."""
    start = time.perf_counter()
    try:
        status = await coro
    except Exception:
        status = "down"
    duration_ms = int((time.perf_counter() - start) * 1000)
    health_check_outcome(name, status)
    logger.info("health_probe", check=name, status=status, duration_ms=duration_ms)
    return status


async def _check_adapters() -> dict[str, str]:
    """Probe every registered backend's health_check, individually bounded."""
    results: dict[str, str] = {}
    for name in registry.list():
        results[name] = await _probe(f"adapter:{name}", _check_adapter(name))
    return results


async def _check_adapter(name: str) -> CheckStatus:
    """One adapter's health_check, timeout-bounded so a hung backend can't
    hang the probe."""
    adapter = registry.get(name)
    if adapter is None:
        return "down"
    await asyncio.wait_for(adapter.health_check(), timeout=_CHECK_TIMEOUT_SECONDS)
    return "ok"
