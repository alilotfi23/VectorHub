"""GET /health against real Postgres and Redis: the full ok/degraded/down
contract, including simulated outages and per-adapter status reporting.

The probe lives on the internal admin app (app.admin), never the public app."""

import time
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.qdrant_adapter import QdrantAdapter
from app.adapters.registry import registry
from app.adapters.weaviate_adapter import WeaviateAdapter
from app.admin import app as admin_app
from app.core.cache import get_redis
from app.core.config import get_settings
from app.db.session import get_session
from app.workers.heartbeat import WORKER_HEARTBEAT_PREFIX


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

    # Point the qdrant/weaviate built-ins at guaranteed-dead URLs so the
    # adapters map is deterministic here (the integration layer may or may not
    # have their session-scoped containers running, and the machine may have
    # local dev servers on the default ports).
    registry.register("qdrant", QdrantAdapter, url="http://127.0.0.1:1")
    registry.register("weaviate", WeaviateAdapter, url="http://127.0.0.1:1")
    admin_app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=admin_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    admin_app.dependency_overrides.clear()
    registry.register("qdrant", QdrantAdapter)
    registry.register("weaviate", WeaviateAdapter)


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


async def test_health_ok_all_dependencies(
    client: AsyncClient, redis_url: str, chroma_backend: None
) -> None:
    """Control plane healthy (Postgres + Redis up, live worker heartbeat,
    chroma registered and healthy) with the qdrant/weaviate built-ins pointed
    at dead URLs -> 200 'degraded': a non-critical backend outage must not
    fail the probe (the spec's degraded contract), and the adapters map
    carries per-backend detail."""
    await _write_heartbeat()
    try:
        resp = await client.get("/health")
    finally:
        await _clear_heartbeats()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["workers"] == "ok"
    assert body["checks"]["adapters"] == {"chroma": "ok", "qdrant": "down", "weaviate": "down"}


async def test_worker_heartbeat_writer_feeds_workers_check(
    client: AsyncClient, redis_url: str
) -> None:
    """The worker-side heartbeat writer (app.workers.heartbeat) is the
    producer the health check consumes: after write_heartbeat, /health
    reports workers ok — the 'check goes green in real deployments' proof."""
    from app.workers.heartbeat import write_heartbeat

    redis = get_redis()
    assert redis is not None
    await write_heartbeat(redis, worker_id="integration-worker")
    try:
        resp = await client.get("/health")
    finally:
        await _clear_heartbeats()
    assert resp.status_code == 200
    body = resp.json()
    # degraded, not ok: the fixture points qdrant/weaviate at dead URLs; the
    # point here is the workers check going green via the real writer.
    assert body["status"] == "degraded"
    assert body["checks"]["workers"] == "ok"


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


async def test_health_reports_adapter_status(
    client: AsyncClient, redis_url: str, chroma_backend: None
) -> None:
    """Per-backend status flows into the probe; a broken backend degrades
    (non-critical) rather than failing the probe. The registered built-in
    (chroma, healthy here) joins the map."""
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
    assert body["checks"]["adapters"] == {
        "broken": "down",
        "chroma": "ok",
        "good": "ok",
        "qdrant": "down",
        "weaviate": "down",
    }


async def test_health_down_when_postgres_unreachable(client: AsyncClient) -> None:
    """A Postgres outage is critical: 503 'down', regardless of the rest."""
    bad_engine = create_async_engine(
        "postgresql+asyncpg://app:app@127.0.0.1:1/nope", pool_pre_ping=True
    )
    bad_factory = async_sessionmaker(bad_engine, expire_on_commit=False)

    async def bad_session() -> AsyncGenerator[AsyncSession, None]:
        async with bad_factory() as session:
            yield session

    admin_app.dependency_overrides[get_session] = bad_session
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
