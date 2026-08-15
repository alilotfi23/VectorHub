"""Worker heartbeat scaffold — the producer side of the workers health check.

Pins the contract health_service consumes (key name, epoch-float value, TTL),
the loop's refresh cadence, and the arq WorkerSettings wiring (hooks that
start/stop the loop around the worker's lifetime).
"""

import asyncio
import time
from typing import Any

import pytest

import app.workers.heartbeat as heartbeat_module
from app.core.config import get_settings
from app.workers import WorkerSettings, _on_shutdown, _on_startup, ping, run_batch_ingest
from app.workers.heartbeat import WORKER_HEARTBEAT_PREFIX, HeartbeatLoop, write_heartbeat


class _FakeRedis:
    """Records set() calls — enough surface for the heartbeat contract."""

    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def set(self, name: str, value: str, **kwargs: Any) -> None:
        self.set_calls.append((name, value, kwargs))


async def test_write_heartbeat_uses_the_health_contract() -> None:
    """The key is vhk:worker:heartbeat:<id>, the value a parseable epoch
    float (exactly what _has_fresh_worker_heartbeat reads), and the TTL
    covers the freshness window."""
    redis = _FakeRedis()
    await write_heartbeat(redis, worker_id="worker-1")  # type: ignore[arg-type]

    assert len(redis.set_calls) == 1
    name, value, kwargs = redis.set_calls[0]
    assert name == f"{WORKER_HEARTBEAT_PREFIX}worker-1"
    ts = float(value)
    assert abs(ts - time.time()) < 30
    assert kwargs["ex"] == get_settings().worker_heartbeat_ttl_seconds


async def test_heartbeat_loop_refreshes_on_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop keeps writing while running and stop() cancels it; a failed
    write is swallowed, never fatal to the loop."""
    writes: list[str] = []

    async def fake_write(redis: object, *, worker_id: str) -> None:
        writes.append(worker_id)

    monkeypatch.setattr(heartbeat_module, "write_heartbeat", fake_write)
    monkeypatch.setattr(get_settings(), "worker_heartbeat_interval_seconds", 0)

    loop = HeartbeatLoop(_FakeRedis())  # type: ignore[arg-type]
    loop.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await loop.stop()

    assert len(writes) >= 2, "heartbeat must refresh on cadence"
    assert all(w == loop.worker_id for w in writes), "one key per worker process"


async def test_worker_settings_scaffold() -> None:
    """The worker is arq-runnable: hooks + redis settings present, and the
    queue-facing functions are registered (arq refuses to start a worker
    with no functions or cron jobs)."""
    assert set(WorkerSettings.functions) == {ping, run_batch_ingest}
    assert WorkerSettings.on_startup is _on_startup
    assert WorkerSettings.on_shutdown is _on_shutdown
    assert WorkerSettings.redis_settings is not None


async def test_ping_function_writes_one_beat() -> None:
    """The queue-facing liveness job writes one beat and returns the worker
    id it used — the manual ping."""
    redis = _FakeRedis()
    worker_id = await ping({"redis": redis})
    assert len(redis.set_calls) == 1
    name, _, kwargs = redis.set_calls[0]
    assert name == f"{WORKER_HEARTBEAT_PREFIX}{worker_id}"
    assert kwargs["ex"] == get_settings().worker_heartbeat_ttl_seconds


async def test_worker_startup_and_shutdown_wire_the_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_startup starts a heartbeat loop against the worker's Redis pool and
    on_shutdown stops it — the wiring that keeps /health's workers check green
    while a worker process is alive."""
    import app.workers as workers_module

    events: list[str] = []

    class _FakeLoop:
        def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

    monkeypatch.setattr(workers_module, "HeartbeatLoop", lambda redis: _FakeLoop())

    await _on_startup({"redis": object()})
    await _on_shutdown({})

    assert events == ["start", "stop"]
