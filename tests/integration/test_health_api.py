"""GET /health against real Postgres and Redis: the full ok/degraded/down
contract, including simulated outages and per-adapter status reporting."""

import time
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.registry import registry
from app.core.cache import get_redis
from app.core.config import get_settings
from app.db.session import get_session
from app.main import app
from app.services.health_service import WORKER_HEARTBEAT_PREFIX


class _HealthyAdapter:
    async def health_check(self) -> None:
        return None


class _BrokenAdapter:
    async def health_check(self) -> None:
        raise RuntimeError("backend unreachable")


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _write_heartbeat(*, ts: float | None = None) -> None:
    redis = get_redis()
    assert redis is not None
    await redis.set(
        f"{WORKER_HEARTBEAT_PREFIX}test-worker",
        str(ts if ts is not None else time.time()),
        ex=60,
    )


async def _clear_heartbeats() -> None:
    redis = get_redis()
    assert redis is not None
    async for key in redis.scan_iter(match=f"{WORKER_HEARTBEAT_PREFIX}*", count=100):
        await redis.delete(key)


async def test_health_ok_all_dependencies(client: AsyncClient, redis_url: str) -> None:
    """Happy path: Postgres + Redis up, a live worker heartbeat -> 200 'ok'."""
    await _write_heartbeat()
    try:
        resp = await client.get("/health")
    finally:
        await _clear_heartbeats()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["workers"] == "ok"
    assert body["checks"]["adapters"] == {}


async def test_health_degraded_without_worker_heartbeat(
    client: AsyncClient, redis_url: str
) -> None:
    """No live worker -> workers 'down' degrades the probe, but it stays 200."""
    await _clear_heartbeats()
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["workers"] == "down"


async def test_health_degraded_with_stale_worker_heartbeat(
    client: AsyncClient, redis_url: str
) -> None:
    """A heartbeat older than the TTL means no live worker."""
    await _clear_heartbeats()
    await _write_heartbeat(ts=time.time() - 120)
    resp = await client.get("/health")
    await _clear_heartbeats()
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"
    assert resp.json()["checks"]["workers"] == "down"


async def test_health_reports_adapter_status(client: AsyncClient, redis_url: str) -> None:
    """Per-backend status flows into the probe; a broken backend degrades
    (non-critical) rather than failing the probe."""
    await _clear_heartbeats()
    registry.register("good", _HealthyAdapter())
    registry.register("broken", _BrokenAdapter())
    try:
        resp = await client.get("/health")
    finally:
        registry.unregister("good")
        registry.unregister("broken")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["adapters"] == {"broken": "down", "good": "ok"}


async def test_health_down_when_postgres_unreachable(client: AsyncClient) -> None:
    """A Postgres outage is critical: 503 'down', regardless of the rest."""
    bad_engine = create_async_engine(
        "postgresql+asyncpg://app:app@127.0.0.1:1/nope", pool_pre_ping=True
    )
    bad_factory = async_sessionmaker(bad_engine, expire_on_commit=False)

    async def bad_session() -> AsyncGenerator[AsyncSession, None]:
        async with bad_factory() as session:
            yield session

    app.dependency_overrides[get_session] = bad_session
    try:
        resp = await client.get("/health")
    finally:
        await bad_engine.dispose()
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "down"
    assert body["checks"]["postgres"] == "down"


async def test_health_down_when_redis_unreachable(
    client: AsyncClient, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Redis outage is critical too: 503 'down' even with Postgres healthy.

    get_redis() rebuilds its singleton when the configured URL changes, so
    pointing settings at a dead endpoint makes the PING fail deterministically.
    """
    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")
    resp = await client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "down"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "down"
