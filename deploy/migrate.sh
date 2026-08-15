#!/usr/bin/env sh
# Control-plane migration bootstrap, idempotent by design.
#
# Runs Alembic as the migrator role (the DDL holder; audit_log's append-only
# guard trigger applies to it too), then sets the app/migrator role passwords
# — the initial migration creates the roles but cannot know their passwords,
# so deployment owns them (mirrors the testcontainers setup in tests/).
# Both steps are safe to re-run: `alembic upgrade head` is a no-op at head,
# and ALTER ROLE ... PASSWORD is idempotent. That property is what lets the
# k8s initContainer pattern and `docker compose run --rm migrate` share this
# one entrypoint.
set -eu

: "${MIGRATOR_DATABASE_URL:?MIGRATOR_DATABASE_URL must be set (superuser URL; Alembic runs as the migrator role)}"
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD must be set}"
: "${POSTGRES_MIGRATOR_PASSWORD:?POSTGRES_MIGRATOR_PASSWORD must be set}"

echo "[migrate] alembic upgrade head"
alembic upgrade head

echo "[migrate] setting app/migrator role passwords"
python - <<'PY'
import asyncio
import os

import asyncpg


async def main() -> None:
    url = os.environ["MIGRATOR_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    conn = await asyncpg.connect(url)
    try:
        for role in ("app", "migrator"):
            # f-string interpolation (single quotes escaped) — ALTER ROLE is a
            # utility statement, so asyncpg's parametrized protocol can't be
            # used for the password.
            password = os.environ[f"POSTGRES_{role.upper()}_PASSWORD"].replace("'", "''")
            await conn.execute(f"ALTER ROLE {role} PASSWORD '{password}'")
    finally:
        await conn.close()


asyncio.run(main())
PY

echo "[migrate] complete"
