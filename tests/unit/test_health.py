from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.services.health_service as health_service
from app.admin import app as admin_app
from app.db.session import get_session
from app.main import app as public_app

admin_client = TestClient(admin_app)
public_client = TestClient(public_app)


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

    # Point the built-in chroma adapter at a guaranteed-dead port so the probe
    # is deterministic regardless of what earlier suites in the process left
    # registered (the integration layer's session-scoped chroma container can
    # still be running here, which would otherwise report "ok").
    from app.adapters.chroma_adapter import ChromaAdapter
    from app.adapters.registry import registry

    registry.register("chroma", ChromaAdapter, url="http://127.0.0.1:1")
    monkeypatch.setattr(health_service, "get_redis", lambda: None)
    admin_app.dependency_overrides[get_session] = override_get_session
    try:
        resp = admin_client.get("/health")
    finally:
        admin_app.dependency_overrides.clear()
        registry.register("chroma", ChromaAdapter)
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "down"
    assert body["checks"]["postgres"] == "down"
    assert body["checks"]["redis"] == "down"
    assert body["checks"]["workers"] == "down"
    # The chroma built-in registers at import (settings default URL, no server
    # running here) — its probe reports down, exactly as a deployment without
    # chroma would.
    assert body["checks"]["adapters"] == {"chroma": "down"}


async def test_health_probe_emits_structured_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every check outcome records one health_probe line (check, status,
    duration_ms) so probe outcomes join the same structured stream as the
    request logs — 429s and probe outcomes traceable in one place. The admin
    app has no middleware, so these lines carry no request_id; correlation is
    by check + timestamp."""
    captured: dict[str, Any] = {}

    class StubLogger:
        def info(self, event: str, **kwargs: Any) -> None:
            captured["event"] = event
            captured.update(kwargs)

    monkeypatch.setattr(health_service, "logger", StubLogger())

    async def _ok() -> health_service.CheckStatus:
        return "ok"

    status = await health_service._probe("postgres", _ok())

    assert status == "ok"
    assert captured["event"] == "health_probe"
    assert captured["check"] == "postgres"
    assert captured["status"] == "ok"
    assert captured["duration_ms"] >= 0


def test_cors_default_permissive() -> None:
    """CORS lives on the public app only (the admin app has no middleware)."""
    resp = public_client.options(
        "/api/v1/auth/register",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
