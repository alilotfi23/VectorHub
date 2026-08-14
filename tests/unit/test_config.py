import pytest
from _pytest.monkeypatch import MonkeyPatch
from pydantic import ValidationError

from app.core.config import Settings


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.vector_max_dimension == 4096
    assert settings.sparse_max_cardinality == 100_000
    assert settings.cors_allowed_origins == "*"
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.jwt_algorithm == "HS256"
    assert settings.jwt_access_ttl_minutes == 15
    assert settings.jwt_refresh_ttl_days == 30
    assert settings.bootstrap_platform_admin_emails == ""


def test_settings_reads_env_overrides(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("VECTOR_MAX_DIMENSION", "512")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.example,https://b.example")
    monkeypatch.setenv("JWT_ACCESS_TTL_MINUTES", "5")
    monkeypatch.setenv("BOOTSTRAP_PLATFORM_ADMIN_EMAILS", "admin@example.com,ops@example.com")
    settings = Settings()
    assert settings.vector_max_dimension == 512
    assert settings.cors_allowed_origins == "https://a.example,https://b.example"
    assert settings.jwt_access_ttl_minutes == 5
    assert settings.bootstrap_platform_admin_emails == "admin@example.com,ops@example.com"


def test_prod_rejects_default_jwt_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="prod", jwt_secret="dev-secret-change-me")
    # A real secret is accepted.
    Settings(environment="prod", jwt_secret="a-long-random-secret-at-least-32-chars")


def test_dev_accepts_default_jwt_secret() -> None:
    Settings(environment="dev", jwt_secret="dev-secret-change-me")


def test_bootstrap_admin_emails_parsed() -> None:
    settings = Settings(bootstrap_platform_admin_emails="A@Example.com, b@example.com")
    assert settings.platform_admin_emails == {"a@example.com", "b@example.com"}
