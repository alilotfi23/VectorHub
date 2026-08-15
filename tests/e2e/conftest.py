"""Shared fixtures for the e2e layer.

The infra fixtures (migrated Postgres, Redis, real vector backends, MinIO,
the middleware-patching session factory) live in the top-level
``tests/conftest.py`` so every layer shares ONE instance of each. This
conftest only layers the e2e ``client`` on top: Layer 3 of the isolation
suite runs the real platform against the real backends.
"""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import get_session
from app.main import app


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    redis_url: str,  # cache-on for the e2e layer (the production path)
    chroma_backend: None,  # Layer 3 runs the real platform against real vector backends
    qdrant_backend: None,
    weaviate_backend: None,
    milvus_backend: None,
    minio_url: str,  # batch staging (E5 enqueues through the real route)
) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
