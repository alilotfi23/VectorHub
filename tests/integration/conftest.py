import asyncio
import os
from collections.abc import AsyncGenerator
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from app.core.config import get_settings

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
async def session_factory(
    db_url: str,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(db_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def db(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session
