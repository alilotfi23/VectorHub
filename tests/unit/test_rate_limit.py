from typing import Any

import pytest

from app.core.config import get_settings
from app.core.exceptions import ErrorCode
from app.core.rate_limit import route_rate
from app.middleware.rate_limit import (
    _LIMIT_ERROR_CODES,
    Rejection,
    _record_rejection,
)


def test_route_rate_override_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_default_qps", 100.0)
    monkeypatch.setattr(settings, "rate_limit_route_qps", {"GET /api/v1/auth/me": 5.0})
    assert route_rate("GET /api/v1/auth/me") == 5.0
    # Any other route falls back to the platform default.
    assert route_rate("POST /api/v1/auth/login") == 100.0


def test_limit_error_codes_cover_all_three_kinds() -> None:
    assert _LIMIT_ERROR_CODES == {
        "route_qps": ErrorCode.RATE_LIMIT_ROUTE_QPS,
        "api_key_qps": ErrorCode.RATE_LIMIT_API_KEY_QPS,
        "tenant_qps": ErrorCode.RATE_LIMIT_TENANT_QPS,
    }


def test_record_rejection_emits_structured_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every 429 emits one structured log line naming the limit hit, with the
    request method/path and the affected tenant/key ids (never the key itself)."""
    import app.middleware.rate_limit as rate_limit_module

    captured: dict[str, Any] = {}

    class StubLogger:
        def warning(self, event: str, **kwargs: Any) -> None:
            captured["event"] = event
            captured.update(kwargs)

    monkeypatch.setattr(rate_limit_module, "logger", StubLogger())

    _record_rejection(
        "GET",
        "/api/v1/auth/me",
        Rejection("tenant_qps", retry_after=3, tenant_id="tenant-1", api_key_id="key-1"),
    )

    assert captured["event"] == "rate_limit_exceeded"
    assert captured["limit"] == "tenant_qps"
    assert captured["retry_after"] == 3
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/auth/me"
    assert captured["tenant_id"] == "tenant-1"
    assert captured["api_key_id"] == "key-1"
