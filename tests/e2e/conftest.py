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
    db,
    db_url,
    redis_url,
    session_factory,
)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811 — pytest fixture name, shadows the conftest re-export
    redis_url: str,  # noqa: F811 — cache-on for the e2e layer (the production path)
) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
