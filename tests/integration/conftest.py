import asyncio
import os
from collections.abc import AsyncGenerator
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

import app.middleware.rate_limit as rate_limit_module
from app.core.cache import close_redis
from app.core.config import get_settings

CHROMA_IMAGE = "chromadb/chroma:1.5.9"  # pinned to the installed client version
QDRANT_IMAGE = "qdrant/qdrant:v1.19.0"  # matches qdrant-client 1.19.0
WEAVIATE_IMAGE = "semitechnologies/weaviate:1.28.4"  # matches weaviate-client 4.23.0

MIGRATION_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "alembic"))
APP_ROLE_PASSWORD = "app_test_password"


@pytest.fixture(scope="session")
async def db_url() -> AsyncGenerator[str, None]:
    """Migrated Postgres; yields the URL the app role connects with.

    Alembic runs as the migrator role against the container's default role,
    then the app role gets a password so the ORM connects as `app` — the same
    split the deployment uses, and the grants/trigger are exercised for real.
    """
    with PostgresContainer("postgres:16-alpine") as pg:
        super_url = pg.get_connection_url(driver="asyncpg")
        os.environ["MIGRATOR_DATABASE_URL"] = super_url
        get_settings.cache_clear()
        cfg = AlembicConfig(os.path.join(MIGRATION_DIR, "..", "alembic.ini"))
        cfg.set_main_option("script_location", MIGRATION_DIR)
        # Alembic's command API calls asyncio.run() internally; run it in a
        # worker thread so it doesn't collide with pytest-asyncio's loop.
        await asyncio.to_thread(command.upgrade, cfg, "head")

        conn = await asyncpg.connect(super_url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            await conn.execute(f"ALTER ROLE app PASSWORD '{APP_ROLE_PASSWORD}'")
        finally:
            await conn.close()

        parsed = urlparse(super_url)
        netloc = f"app:{APP_ROLE_PASSWORD}@{parsed.hostname}:{parsed.port}"
        yield urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


@pytest.fixture(scope="session")
async def redis_url() -> AsyncGenerator[str, None]:
    """Redis testcontainer; REDIS_URL is set so the auth caches engage.

    The e2e layer depends on this fixture (cache-on is the production path)
    and cache-behavior tests opt in explicitly; every other suite runs
    cache-off so the Postgres-fallback path stays covered.
    """
    with RedisContainer("redis:7-alpine") as redis:
        url = f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}"
        # Startup-race guard: the host port binds a moment after the container
        # starts, so under batch load a test can hit ConnectionRefusedError
        # before the proxy is up. Wait until Redis actually answers a PING.
        probe = AsyncRedis.from_url(url)
        try:
            for _ in range(50):
                try:
                    await probe.ping()
                    break
                except Exception:
                    await asyncio.sleep(0.2)
            else:
                raise RuntimeError("Redis testcontainer did not become ready in time")
        finally:
            await probe.aclose()
        os.environ["REDIS_URL"] = url
        get_settings.cache_clear()
        # Deterministic re-point: a client built earlier in the process (e.g.
        # before this fixture activated, or against another URL) must not
        # survive into the cache tests, or writes would go to a dead endpoint.
        await close_redis()
        yield url
        os.environ.pop("REDIS_URL", None)
        get_settings.cache_clear()
        await close_redis()


@pytest.fixture(scope="session")
async def session_factory(
    db_url: str,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(db_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # The rate-limit middleware resolves tenant/key rate config through its
    # own session factory; point it at the test DB so every authenticated
    # request doesn't attempt the default (dead) engine. Restored on teardown.
    original_factory = rate_limit_module.session_factory
    rate_limit_module.session_factory = factory
    try:
        yield factory
    finally:
        rate_limit_module.session_factory = original_factory
        await engine.dispose()


@pytest.fixture(scope="session")
async def chroma_url() -> AsyncGenerator[str, None]:
    """Chroma server testcontainer (image pinned to the installed client).

    Lazy: only starts when a test requests it (collection/vector suites). The
    process singleton registry otherwise points at the settings default, so
    suites that never touch a vector backend pay no Docker cost.
    """
    with DockerContainer(CHROMA_IMAGE).with_exposed_ports(8000) as container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8000)}"
        # Startup-race guard (same pattern as the redis fixture): poll the
        # server heartbeat until it answers.
        import httpx

        for _ in range(60):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{url}/api/v2/heartbeat", timeout=2)
                if resp.status_code == 200:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Chroma testcontainer did not become ready in time")
        yield url


@pytest.fixture(scope="session")
async def chroma_backend(chroma_url: str) -> AsyncGenerator[None, None]:
    """Point the process-wide 'chroma' adapter at the test container (the
    built-in registered at import uses the settings default). Restores the
    default on teardown."""
    from app.adapters.chroma_adapter import ChromaAdapter
    from app.adapters.registry import registry

    registry.register("chroma", ChromaAdapter, url=chroma_url)
    yield
    registry.register("chroma", ChromaAdapter)


@pytest.fixture(scope="session")
async def qdrant_url() -> AsyncGenerator[str, None]:
    """Qdrant server testcontainer (image pinned to the installed client)."""
    with DockerContainer(QDRANT_IMAGE).with_exposed_ports(6333) as container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(6333)}"
        import httpx

        for _ in range(60):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{url}/readyz", timeout=2)
                if resp.status_code == 200:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Qdrant testcontainer did not become ready in time")
        yield url


@pytest.fixture(scope="session")
async def qdrant_backend(qdrant_url: str) -> AsyncGenerator[None, None]:
    from app.adapters.qdrant_adapter import QdrantAdapter
    from app.adapters.registry import registry

    registry.register("qdrant", QdrantAdapter, url=qdrant_url)
    yield
    registry.register("qdrant", QdrantAdapter)


@pytest.fixture(scope="session")
async def weaviate_url() -> AsyncGenerator[tuple[str, int], None]:
    """Weaviate server testcontainer; yields (http_url, grpc_port) — the
    adapter needs both (the gRPC channel is the query transport)."""
    with DockerContainer(WEAVIATE_IMAGE).with_exposed_ports(8080, 50051) as container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        grpc_port = container.get_exposed_port(50051)
        import httpx

        for _ in range(90):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{url}/v1/.well-known/ready", timeout=2)
                if resp.status_code == 200:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Weaviate testcontainer did not become ready in time")
        yield url, grpc_port


@pytest.fixture(scope="session")
async def weaviate_backend(weaviate_url: tuple[str, int]) -> AsyncGenerator[None, None]:
    from app.adapters.registry import registry
    from app.adapters.weaviate_adapter import WeaviateAdapter

    url, grpc_port = weaviate_url
    registry.register("weaviate", WeaviateAdapter, url=url, grpc_port=grpc_port)
    yield
    registry.register("weaviate", WeaviateAdapter)


@pytest.fixture
async def db(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session
