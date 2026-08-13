from _pytest.monkeypatch import MonkeyPatch

from app.core.config import Settings


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.vector_max_dimension == 4096
    assert settings.sparse_max_cardinality == 100_000
    assert settings.cors_allowed_origins == "*"
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_settings_reads_env_overrides(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("VECTOR_MAX_DIMENSION", "512")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.example,https://b.example")
    settings = Settings()
    assert settings.vector_max_dimension == 512
    assert settings.cors_allowed_origins == "https://a.example,https://b.example"
