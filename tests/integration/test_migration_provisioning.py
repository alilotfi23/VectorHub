import asyncio
import os
from collections.abc import AsyncGenerator

import asyncpg  # type: ignore[import-untyped]
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from testcontainers.postgres import PostgresContainer

from app.core.config import get_settings

MIGRATION_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "alembic"))


def _run_upgrade(cfg: AlembicConfig) -> None:
    # Alembic's command API calls asyncio.run() internally, which cannot run
    # inside pytest-asyncio's loop — execute it in a worker thread instead.
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
async def migrated_url() -> AsyncGenerator[str, None]:
    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url(driver="asyncpg")
        os.environ["MIGRATOR_DATABASE_URL"] = url
        get_settings.cache_clear()
        cfg = AlembicConfig(os.path.join(MIGRATION_DIR, "..", "alembic.ini"))
        cfg.set_main_option("script_location", MIGRATION_DIR)
        await asyncio.to_thread(_run_upgrade, cfg)
        # Seed a tenant the raw-SQL tests reference (idempotent across tests).
        conn = await _connect(url)
        try:
            await conn.execute(
                "INSERT INTO tenants (id, name, created_at, updated_at) "
                "VALUES ('t', 'test-tenant', now(), now()) "
                "ON CONFLICT (id) DO NOTHING"
            )
        finally:
            await conn.close()
        yield url


async def _connect(url: str) -> asyncpg.Connection:
    return await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))


async def test_roles_and_trigger_created(migrated_url: str) -> None:
    conn = await _connect(migrated_url)
    try:
        roles = await conn.fetch(
            "SELECT rolname FROM pg_roles WHERE rolname IN ('app', 'migrator')"
        )
        assert {r["rolname"] for r in roles} == {"app", "migrator"}

        triggers = await conn.fetch(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = 'audit_log'::regclass AND NOT tgisinternal"
        )
        assert "audit_log_no_update_delete" in {t["tgname"] for t in triggers}
    finally:
        await conn.close()


async def test_app_role_append_only_audit_log(migrated_url: str) -> None:
    conn = await _connect(migrated_url)
    try:
        await conn.execute("SET ROLE app")
        row_id = await conn.fetchval(
            "INSERT INTO audit_log "
            "(id, tenant_id, action, resource_type, details, result, created_at) "
            "VALUES ('a', 't', 'action', 'type', '{}'::json, 'success', now()) RETURNING id"
        )
        assert row_id == "a"

        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("UPDATE audit_log SET result = 'failed' WHERE id = 'a'")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM audit_log WHERE id = 'a'")
    finally:
        await conn.close()


async def test_guard_trigger_blocks_migrator(migrated_url: str) -> None:
    # migrator holds full table rights (it is the DDL role), so the ONLY thing
    # blocking UPDATE/DELETE on audit_log must be the guard trigger.
    conn = await _connect(migrated_url)
    try:
        await conn.execute("SET ROLE migrator")
        await conn.execute(
            "INSERT INTO audit_log "
            "(id, tenant_id, action, resource_type, details, result, created_at) "
            "VALUES ('b', 't', 'action', 'type', '{}'::json, 'success', now())"
        )  # INSERT is not blocked — the trigger is BEFORE UPDATE OR DELETE
        with pytest.raises(asyncpg.exceptions.RaiseError):
            await conn.execute("UPDATE audit_log SET result = 'failed' WHERE id = 'b'")
        with pytest.raises(asyncpg.exceptions.RaiseError):
            await conn.execute("DELETE FROM audit_log WHERE id = 'b'")
    finally:
        await conn.close()


async def test_app_role_can_crud_collections(migrated_url: str) -> None:
    conn = await _connect(migrated_url)
    try:
        await conn.execute("SET ROLE app")
        await conn.execute(
            "INSERT INTO collections (id, tenant_id, name, backend, dimension, "
            "distance_metric, physical_name, metadata, created_at, updated_at) "
            "VALUES ('c', 't', 'n', 'chroma', 16, 'cosine', 'col_x', '{}'::json, now(), now())"
        )
        await conn.execute("UPDATE collections SET status = 'active' WHERE id = 'c'")
        await conn.execute("DELETE FROM collections WHERE id = 'c'")
    finally:
        await conn.close()
