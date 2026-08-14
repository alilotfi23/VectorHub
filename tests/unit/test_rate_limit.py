import pytest

from app.core.config import get_settings
from app.core.exceptions import ErrorCode
from app.core.rate_limit import route_rate
from app.middleware.rate_limit import _LIMIT_ERROR_CODES


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
