"""Worker heartbeat — the producer side of the workers health check.

``app.services.health_service`` treats any ``vhk:worker:heartbeat:<id>`` key
newer than ``worker_heartbeat_ttl_seconds`` as a live worker. This module is
what writes those keys: one key per worker process (``<id>`` is a fresh
UUID4 hex), refreshed on a cadence well under the TTL so transient hiccups
can't age the key out, and expired via Redis TTL when the worker dies.
"""

import asyncio
import time
import uuid

from redis.asyncio import Redis

from app.core.config import get_settings

WORKER_HEARTBEAT_PREFIX = "vhk:worker:heartbeat:"


async def write_heartbeat(redis: Redis, *, worker_id: str) -> None:
    """Write one fresh heartbeat for ``worker_id``.

    The value is the epoch timestamp as seconds (what the health probe parses
    via ``float``); the TTL is the freshness window itself, so a dead worker's
    key expires on its own and a briefly-stalled worker still has old beats
    within the window before the probe calls it down.
    """
    ttl = get_settings().worker_heartbeat_ttl_seconds
    await redis.set(
        f"{WORKER_HEARTBEAT_PREFIX}{worker_id}",
        str(time.time()),
        ex=max(ttl, 1),
    )


class HeartbeatLoop:
    """A cancellable background task writing heartbeats on a cadence.

    One loop per worker process. A Redis failure mid-loop is swallowed — the
    worker keeps trying and the health probe reports ``workers`` down until
    Redis recovers; a failed heartbeat must never kill the worker.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._worker_id = uuid.uuid4().hex
        self._task: asyncio.Task[None] | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def start(self) -> None:
        """Spawn the loop. Safe to call more than once (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the loop and wait for it to wind down."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        interval = get_settings().worker_heartbeat_interval_seconds
        while True:
            try:
                await write_heartbeat(self._redis, worker_id=self._worker_id)
            except Exception:
                pass
            await asyncio.sleep(interval)
