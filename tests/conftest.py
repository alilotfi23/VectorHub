"""Shared infra fixtures for every test layer (unit/integration/e2e).

These fixtures must live in the *top-level* tests/conftest.py, not a
layer conftest: pytest registers a conftest's fixtures under that conftest's
directory path, so a fixture re-exported from one layer's conftest (e.g.
``from tests.integration.conftest import session_factory``) registers a
*second* FixtureDef — and with it a second session-scoped instance: a second
Postgres container, second engine, second middleware patch cycle, second
vector-DB registration. While layers never interleave (e2e runs before
integration), that split is invisible; pytest-random-order interleaves the
layers, and the registry/middleware state flips between the two worlds
mid-session — e.g. the audit middleware writes to the e2e Postgres where the
integration-created tenant doesn't exist (ForeignKeyViolationError), and the
rate limiter's tenant-cap lookup misses. See the random-order CI job.

Containers stay lazy: each fixture only starts when a test requests it, so
unit tests pay no Docker cost.
"""

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

import app.middleware.audit as audit_module
import app.middleware.rate_limit as rate_limit_module
from app.core.cache import close_redis
from app.core.config import get_settings

CHROMA_IMAGE = "chromadb/chroma:1.5.9"  # pinned to the installed client version
QDRANT_IMAGE = "qdrant/qdrant:v1.19.0"  # matches qdrant-client 1.19.0
WEAVIATE_IMAGE = "semitechnologies/weaviate:1.28.4"  # matches weaviate-client 4.23.0
# Milvus standalone needs etcd + MinIO sidecars (the CLAUDE.md policy: its
# suite is heavier/slower — separate CI job). Images mirror the official
# milvus standalone compose; milvus 3.0.0 matches pymilvus 3.0.1.
MILVUS_ETCD_IMAGE = "quay.io/coreos/etcd:v3.5.25"
MILVUS_MINIO_IMAGE = "minio/minio:RELEASE.2024-05-28T17-19-04Z"
MILVUS_IMAGE = "milvusdb/milvus:v3.0.0"
MINIO_IMAGE = "minio/minio:RELEASE.2024-05-28T17-19-04Z"

MIGRATION_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic"))
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
    # The rate-limit and audit middlewares resolve through their own session
    # factories; point both at the test DB so every authenticated request
    # (and every failed-write audit row) hits the migrated test Postgres
    # instead of the default (dead) engine. Restored on teardown.
    original_factory = rate_limit_module.session_factory
    original_audit_factory = audit_module.session_factory
    rate_limit_module.session_factory = factory
    audit_module.session_factory = factory
    try:
        yield factory
    finally:
        rate_limit_module.session_factory = original_factory
        audit_module.session_factory = original_audit_factory
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


@pytest.fixture(scope="session")
async def minio_url() -> AsyncGenerator[str, None]:
    """MinIO testcontainer for batch staging (the Phase 6 async-batch path;
    the same image the Milvus trio uses as its object-store sidecar). Yields
    the S3 endpoint URL; credentials are the defaults (minioadmin)."""
    with (
        DockerContainer(MINIO_IMAGE)
        .with_exposed_ports(9000)
        .with_env("MINIO_ACCESS_KEY", "minioadmin")
        .with_env("MINIO_SECRET_KEY", "minioadmin")
        .with_command("minio server /minio_data --console-address :9001")
    ) as container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        import boto3  # type: ignore[import-untyped]

        client = boto3.client(
            "s3",
            endpoint_url=url,
            aws_access_key_id="minioadmin",
            aws_secret_access_key="minioadmin",
            region_name="us-east-1",
        )
        for _ in range(60):
            try:
                client.list_buckets()
                break
            except Exception:
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("MinIO testcontainer did not become ready in time")
        # Point BATCH_STORAGE_* at the test container so every BatchStorage()
        # constructed from settings (e.g. the route's JobService) hits MinIO
        # here — the production env-driven path, exercised for real.
        os.environ["BATCH_STORAGE_ENDPOINT"] = url
        os.environ["BATCH_STORAGE_ACCESS_KEY"] = "minioadmin"
        os.environ["BATCH_STORAGE_SECRET_KEY"] = "minioadmin"
        os.environ["BATCH_STORAGE_BUCKET"] = "vectorhub-batches"
        get_settings.cache_clear()
        yield url
        for var in (
            "BATCH_STORAGE_ENDPOINT",
            "BATCH_STORAGE_ACCESS_KEY",
            "BATCH_STORAGE_SECRET_KEY",
            "BATCH_STORAGE_BUCKET",
        ):
            os.environ.pop(var, None)
        get_settings.cache_clear()


@pytest.fixture(scope="session")
async def milvus_url() -> AsyncGenerator[str, None]:
    """Milvus standalone testcontainer trio (etcd + MinIO sidecars on a shared
    network with aliases, per the official standalone compose). Yields the
    gRPC URL; readiness is probed with the AsyncMilvusClient itself (Milvus
    takes 1-2 min to come up). Only tests that request it pay the Docker cost.
    """
    from testcontainers.core.network import Network

    net = Network()
    net.create()
    etcd = (
        DockerContainer(MILVUS_ETCD_IMAGE)
        .with_network(net)
        .with_network_aliases("etcd")
        .with_command(
            "etcd -advertise-client-urls=http://etcd:2379 "
            "-listen-client-urls http://0.0.0.0:2379 --data-dir /etcd"
        )
    )
    minio = (
        DockerContainer(MILVUS_MINIO_IMAGE)
        .with_network(net)
        .with_network_aliases("minio")
        .with_env("MINIO_ACCESS_KEY", "minioadmin")
        .with_env("MINIO_SECRET_KEY", "minioadmin")
        .with_command("minio server /minio_data --console-address :9001")
    )
    milvus = (
        DockerContainer(MILVUS_IMAGE)
        .with_network(net)
        .with_network_aliases("milvus")
        .with_env("ETCD_ENDPOINTS", "etcd:2379")
        .with_env("MINIO_ADDRESS", "minio:9000")
        .with_exposed_ports(19530)
        .with_command(["milvus", "run", "standalone"])
    )
    try:
        etcd.start()
        minio.start()
        milvus.start()
        url = f"http://{milvus.get_container_host_ip()}:{milvus.get_exposed_port(19530)}"

        from pymilvus import AsyncMilvusClient

        probe = AsyncMilvusClient(uri=url, timeout=3)
        try:
            for _ in range(150):  # up to ~5 min; Milvus standalone is slow to start
                try:
                    await probe.list_collections()
                    break
                except Exception:
                    await asyncio.sleep(2)
            else:
                raise RuntimeError("Milvus testcontainer trio did not become ready in time")
        finally:
            await probe.close()
        yield url
    finally:
        # Stop the containers BEFORE removing the network: the Docker daemon
        # refuses to remove a network with active endpoints (verified on this
        # box: 403 "network has active endpoints"). Reverse order of start.
        milvus.stop()
        minio.stop()
        etcd.stop()
        net.remove()


@pytest.fixture(scope="session")
async def milvus_backend(milvus_url: str) -> AsyncGenerator[None, None]:
    from app.adapters.milvus_adapter import MilvusAdapter
    from app.adapters.registry import registry

    registry.register("milvus", MilvusAdapter, url=milvus_url)
    yield
    registry.register("milvus", MilvusAdapter)


@pytest.fixture
async def db(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session
