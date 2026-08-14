from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.services.health_service as health_service
from app.db.session import get_session
from app.main import app

client = TestClient(app)


class _BrokenSession:
    """Session whose execute always fails — stands in for an unreachable DB."""

    async def execute(self, *args: object, **kwargs: object) -> Any:
        raise ConnectionError("postgres unreachable")


def test_health_down_without_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """With Postgres and Redis both unavailable the probe is 503 'down'.

    Hermetic by construction: the session and the Redis client are stubbed,
    so no real connection is made and the result is deterministic regardless
    of what other suites in the process have configured (e.g. a live
    REDIS_URL from the integration layer's session fixture).
    """

    async def override_get_session() -> Any:
        yield _BrokenSession()

    monkeypatch.setattr(health_service, "get_redis", lambda: None)
    app.dependency_overrides[get_session] = override_get_session
    try:
        resp = client.get("/health")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "down"
    assert body["checks"]["postgres"] == "down"
    assert body["checks"]["redis"] == "down"
    assert body["checks"]["workers"] == "down"
    assert body["checks"]["adapters"] == {}


def test_cors_default_permissive() -> None:
    resp = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
