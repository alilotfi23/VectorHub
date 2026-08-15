"""Shared fixtures for the e2e layer.

Re-exports the migrated-real-Postgres fixtures from the integration suite:
Layer 3 of the isolation suite is control-plane (no vector backend needed),
so it runs against the same migrated Postgres + ASGI app the integration
layer uses. The design doc places Layer 3 at tests/e2e/.
"""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import get_session
from app.main import app
from tests.integration.conftest import (  # noqa: F401
    chroma_backend,
    chroma_url,
    db,
    db_url,
    milvus_backend,
    milvus_url,
    minio_url,
    qdrant_backend,
    qdrant_url,
    redis_url,
    session_factory,
    weaviate_backend,
    weaviate_url,
)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811 — pytest fixture name, shadows the conftest re-export
    redis_url: str,  # noqa: F811 — cache-on for the e2e layer (the production path)
    chroma_backend: None,  # noqa: F811 — Layer 3 runs the real platform against real vector backends; the e2e layer must never depend on an earlier integration suite having registered the containers
    qdrant_backend: None,  # noqa: F811
    weaviate_backend: None,  # noqa: F811
    milvus_backend: None,  # noqa: F811
    minio_url: str,  # noqa: F811 — batch staging (E5 enqueues through the real route, which stages to object storage)
) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
