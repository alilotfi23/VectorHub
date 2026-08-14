from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. Env var names are field names uppercased (e.g. DATABASE_URL)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "vectorhub-platform"
    environment: str = "dev"

    # --- Auth / JWT (Phase 2) ---
    # HS256 with a strong secret. The dev default is a trap: prod refuses to
    # boot with it (see validator below). Generate with `openssl rand -hex 32`.
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 30
    # Comma-separated emails granted is_platform_admin at registration time.
    # Set before the first register in a new deployment; empty in dev means
    # no platform admin exists (POST /tenants stays admin-gated).
    bootstrap_platform_admin_emails: str = ""

    @model_validator(mode="after")
    def _prod_needs_real_secret(self) -> "Settings":
        if self.environment in {"staging", "prod"} and self.jwt_secret == "dev-secret-change-me":
            raise ValueError("JWT_SECRET must be set to a real secret in staging/prod")
        return self

    @property
    def platform_admin_emails(self) -> set[str]:
        emails = (e.strip().lower() for e in self.bootstrap_platform_admin_emails.split(","))
        return {e for e in emails if e}

    # Postgres. The runtime app connects as the `app` role; Alembic migrations
    # run as the `migrator` role (holds DDL, sees the audit_log guard trigger).
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/vectorhub"
    migrator_database_url: str = "postgresql+asyncpg://migrator:migrator@localhost:5432/vectorhub"

    # None = cache disabled (Postgres-fallback auth path). Compose/dev set
    # REDIS_URL explicitly; an unset var must never silently point at a
    # dead default endpoint.
    redis_url: str | None = None
    # TTL for cached auth lookups (API-key principal, jti deny-list marker).
    # Revocation invalidates immediately regardless; the TTL only bounds how
    # long a *valid* resolution stays cached. Postgres remains the source of
    # truth — the cache is an optimization, never a gate.
    auth_cache_ttl_seconds: int = 300
    # Worker heartbeat freshness for GET /health's workers check. The Phase 6
    # arq worker writes vhk:worker:heartbeat:<id> = <epoch-ts>; any heartbeat
    # newer than this means a live worker.
    worker_heartbeat_ttl_seconds: int = 30

    # --- Rate limiting (Phase 6 pull-forward) ---
    # Platform-wide QPS ceiling per route, plus per-route overrides keyed by
    # "METHOD /path" (e.g. {"POST /api/v1/auth/login": 5}). Tenant caps come
    # from tenants.rate_limit_qps and API-key caps from api_keys.rate_limit_qps
    # when set; the most restrictive applicable limit wins per request.
    rate_limit_default_qps: float = 100.0
    rate_limit_route_qps: dict[str, float] = {}
    rate_limit_burst_multiplier: float = 2.0
    # TTL for cached tenant/key rate-config read-throughs (Redis miss ->
    # Postgres -> cached, including negative entries). Bounds how long a
    # rate change takes to apply.
    rate_limit_config_cache_ttl_seconds: int = 300

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
