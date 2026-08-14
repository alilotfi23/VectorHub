"""arq worker settings for the VectorHub platform.

Run with ``arq app.workers.WorkerSettings`` (or ``python -m app.workers``).
Phase 6 fills ``functions`` with the async batch-job tasks (vectors/batch);
this scaffold owns the worker lifecycle and the heartbeat that makes
``GET /health``'s workers check report "ok" in real deployments: while a
worker process is alive it refreshes ``vhk:worker:heartbeat:<id>`` every
``worker_heartbeat_interval_seconds``, and the health probe treats any
heartbeat fresher than ``worker_heartbeat_ttl_seconds`` as a live worker.
"""

import uuid
from typing import Any

from arq.connections import RedisSettings

from app.core.config import get_settings
from app.workers.heartbeat import HeartbeatLoop, write_heartbeat

# The heartbeat loop lives for the worker process's lifetime; the arq context
# is a fresh dict per worker, so it is held here and torn down on shutdown.
_heartbeat: HeartbeatLoop | None = None


def _redis_settings() -> RedisSettings:
    url = get_settings().redis_url
    return RedisSettings.from_dsn(url) if url else RedisSettings()


async def _on_startup(ctx: dict[str, Any]) -> None:
    """Start the heartbeat loop once the worker's Redis pool is up."""
    global _heartbeat
    _heartbeat = HeartbeatLoop(ctx["redis"])
    _heartbeat.start()


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    """Stop the heartbeat loop; the key expires via its own TTL."""
    global _heartbeat
    if _heartbeat is not None:
        await _heartbeat.stop()
        _heartbeat = None


async def ping(ctx: dict[str, Any]) -> str:
    """Write one heartbeat and return the worker id it used.

    Satisfies arq's at-least-one-function requirement (a worker with no
    functions or cron jobs refuses to start) and doubles as a manual liveness
    ping: ``arq app.workers.WorkerSettings`` then ``await ping()``. The
    production cadence is HeartbeatLoop — a per-worker background task that
    keeps the key fresh regardless of queue load — this is the queue-facing
    counterpart. Named ``ping`` (not ``heartbeat``) so it can't shadow the
    ``app.workers.heartbeat`` submodule for dotted imports.
    """
    worker_id = uuid.uuid4().hex
    await write_heartbeat(ctx["redis"], worker_id=worker_id)
    return worker_id


class WorkerSettings:
    # arq's WorkerSettingsType is a Protocol it can't fully satisfy statically
    # (loose stubs, no py.typed) — the entrypoint ignores that one call. This
    # class is consumed by arq's own CLI (`arq app.workers.WorkerSettings`).
    # Phase 6 adds the batch-job tasks here (e.g. run_batch_ingest).
    functions: list[Any] = [ping]
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    redis_settings = _redis_settings()
