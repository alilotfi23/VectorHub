"""Rate-limit enforcement over HTTP: route/tenant/API-key token buckets,
most-restrictive-wins 429s, refill, and fail-open behavior."""

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import get_redis
from app.core.config import get_settings
from app.core.rate_limit import RL_CONFIG_PREFIX
from app.db.models import Tenant
from app.db.session import get_session
from app.main import app

ME_ROUTE = "GET /api/v1/auth/me"


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    # The middleware's session factory is already pointed at the test DB by
    # the shared session_factory fixture (tests/integration/conftest.py).
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _register(client: AsyncClient) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"rl-{time.time_ns()}@example.com",
            "password": "password-123",
            "tenant_name": f"rl-tenant-{time.time_ns()}",
        },
    )
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


async def _set_tenant_rate(db: AsyncSession, tenant_id: str, qps: int) -> None:
    tenant = await db.get(Tenant, tenant_id)
    assert tenant is not None
    tenant.rate_limit_qps = qps
    await db.commit()


def _assert_429(resp: Response, error_code: str, limit: str) -> None:
    assert resp.status_code == 429
    body = resp.json()
    assert body["error_code"] == error_code
    assert body["details"] == {"limit": limit}
    assert int(resp.headers["retry-after"]) >= 1


async def test_route_limit_429_then_refills(
    client: AsyncClient, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route bucket (rate 1, burst 2): two 200s, then 429 naming route_qps;
    the bucket refills and the next request passes."""
    monkeypatch.setattr(get_settings(), "rate_limit_route_qps", {ME_ROUTE: 1.0})
    auth = await _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    for _ in range(2):
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200
    resp = await client.get("/api/v1/auth/me", headers=headers)
    _assert_429(resp, "RATE_LIMIT_ROUTE_QPS", "route_qps")

    # Refill: 1.5s at 1 token/s restores >= 1 token.
    await asyncio.sleep(1.5)
    resp = await client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200


async def test_tenant_limit_most_restrictive_wins(
    client: AsyncClient, redis_url: str, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tenant cap (1 qps) beats the route default (100): the 429 names
    tenant_qps, and the rate config is cached in Redis read-through."""
    auth = await _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    tenant_id = auth["user"]["tenant_id"]
    await _set_tenant_rate(db, tenant_id, qps=1)

    for _ in range(2):
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200
    resp = await client.get("/api/v1/auth/me", headers=headers)
    _assert_429(resp, "RATE_LIMIT_TENANT_QPS", "tenant_qps")

    redis = get_redis()
    assert redis is not None
    cached = await redis.get(f"{RL_CONFIG_PREFIX}tenant:{tenant_id}")
    assert cached == "1.0"


async def test_api_key_limit_429(client: AsyncClient, redis_url: str) -> None:
    """A per-key cap (1 qps) is enforced for key principals and names
    api_key_qps on the 429."""
    auth = await _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    tenant_id = auth["user"]["tenant_id"]

    resp = await client.post(
        "/api/v1/api-keys",
        headers=headers,
        json={"name": "rl-key", "role": "viewer", "rate_limit_qps": 1},
    )
    assert resp.status_code == 201, resp.text
    key = resp.json()["key"]
    key_headers = {"X-API-Key": key}

    for _ in range(2):
        resp = await client.get(f"/api/v1/tenants/{tenant_id}", headers=key_headers)
        assert resp.status_code == 200
    resp = await client.get(f"/api/v1/tenants/{tenant_id}", headers=key_headers)
    _assert_429(resp, "RATE_LIMIT_API_KEY_QPS", "api_key_qps")


async def test_fail_open_when_redis_unconfigured(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With Redis unconfigured the limiter lets everything through — rate
    limiting is a mitigation, never an availability gate."""
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "rate_limit_route_qps", {ME_ROUTE: 1.0})
    auth = await _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    for _ in range(5):
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200
