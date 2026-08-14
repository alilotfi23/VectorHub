"""The metrics surface: the public app records request counts (with
post-routing route labels, including 4xx/5xx and rate-limit 429s) and the
internal admin app exposes them at /metrics, alongside health-check outcomes.
The probe and scrape endpoints never appear on the public app."""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import app as admin_app
from app.core.config import get_settings
from app.db.session import get_session
from app.main import app as public_app


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """The public API app — the traffic source under test."""

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    public_app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=public_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    public_app.dependency_overrides.clear()


@pytest.fixture
async def admin_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """The internal admin app — serves /health and /metrics only."""

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    admin_app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=admin_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    admin_app.dependency_overrides.clear()


async def test_public_app_never_serves_infra_endpoints(client: AsyncClient) -> None:
    """Public traffic gets 404 on the probe/scrape endpoints — they live on
    the internal admin app only."""
    assert (await client.get("/health")).status_code == 404
    assert (await client.get("/metrics")).status_code == 404


async def test_metrics_counts_requests_and_health_outcomes(
    client: AsyncClient, admin_client: AsyncClient, redis_url: str
) -> None:
    """Public-app traffic (401, 404, 2xx) shows up in request counts with
    collapsed route labels; a health probe records per-check outcomes; the
    admin scrape renders both plus the instrumentator series."""
    await client.get("/api/v1/tenants/not-a-tenant")  # no auth -> 401
    await client.get("/definitely-not-a-route")  # unmatched -> 404
    await client.post(
        "/api/v1/auth/register",
        json={
            "email": "metrics-ok@example.com",
            "password": "password-123",
            "tenant_name": "metrics-ok-tenant",
        },
    )  # 201 -> 2xx for the instrumentator series
    await admin_client.get("/health")  # postgres+redis ok, workers down (no heartbeat)

    resp = await admin_client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text

    # Request counts: matched route collapsed to the template, unmatched to raw.
    assert (
        'vhk_requests_total{method="GET",path="/api/v1/tenants/{tenant_id}",status="401"}' in body
    )
    assert 'vhk_requests_total{method="GET",path="/definitely-not-a-route",status="404"}' in body
    # Health outcomes: postgres/redis healthy, workers down in this session.
    assert 'vhk_health_checks_total{check="postgres",status="ok"}' in body
    assert 'vhk_health_checks_total{check="redis",status="ok"}' in body
    assert 'vhk_health_checks_total{check="workers",status="down"}' in body

    # Instrumentator series (Phase 7 pull-forward): the standard request
    # counter (status grouped, handler templated) plus duration histograms
    # and request/response size summaries — recorded for the public requests
    # above. The register call provides the 2xx sample.
    assert "http_requests_total" in body
    assert (
        'http_requests_total{handler="/api/v1/tenants/{tenant_id}",method="GET",status="4xx"}'
        in body
    )
    assert 'status="2xx"' in body
    assert "http_request_duration_seconds_bucket" in body
    assert "http_request_duration_highr_seconds_bucket" in body
    assert 'le="+Inf"' in body
    assert "http_request_size_bytes_count" in body  # Summary: _count/_sum, no buckets
    assert "http_response_size_bytes_count" in body


async def test_metrics_counts_rate_limited_requests(
    client: AsyncClient, admin_client: AsyncClient, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 from the rate-limit middleware is a request too — and it is
    counted (the metrics middleware wraps the rate-limit middleware)."""
    monkeypatch.setattr(get_settings(), "rate_limit_route_qps", {"GET /api/v1/auth/me": 1.0})
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "metrics-429@example.com",
            "password": "password-123",
            "tenant_name": "metrics-429-tenant",
        },
    )
    assert resp.status_code == 201, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    for _ in range(3):  # burst 2 -> third is 429
        await client.get("/api/v1/auth/me", headers=headers)

    metrics = await admin_client.get("/metrics")
    assert metrics.status_code == 200
    assert 'vhk_requests_total{method="GET",path="/api/v1/auth/me",status="429"}' in metrics.text
    assert 'vhk_rate_limit_rejections_total{limit="route_qps"}' in metrics.text
