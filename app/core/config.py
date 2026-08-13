from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. Env var names are field names uppercased (e.g. DATABASE_URL)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "vectorhub-platform"
    environment: str = "dev"

    # Postgres. The runtime app connects as the `app` role; Alembic migrations
    # run as the `migrator` role (holds DDL, sees the audit_log guard trigger).
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/vectorhub"
    migrator_database_url: str = "postgresql+asyncpg://migrator:migrator@localhost:5432/vectorhub"

    redis_url: str = "redis://localhost:6379/0"

    # "*" for local dev; explicit comma-separated allow-list for staging/prod.
    cors_allowed_origins: str = "*"

    # Vector contract limits — Pydantic schema enforcement lands with routes in Phase 3.
    vector_max_dimension: int = 4096
    sparse_max_cardinality: int = 100_000

    # Async batch staging (MinIO in compose / S3 in cloud mode) — used from Phase 6.
    batch_storage_endpoint: str = "http://localhost:9000"
    batch_storage_access_key: str = "minioadmin"
    batch_storage_secret_key: str = "minioadmin"
    batch_storage_bucket: str = "vectorhub-batches"

    # Vector backends — adapters consume these from Phase 3 onward.
    qdrant_url: str = "http://localhost:6333"
    weaviate_url: str = "http://localhost:8080"
    milvus_url: str = "http://localhost:19530"
    chroma_url: str = "http://localhost:8000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
