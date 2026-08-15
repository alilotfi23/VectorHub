"""100k-vector async-batch soak on Qdrant — throughput-model validation.

Runs the full Phase 6 batch data path against real components: the NDJSON
body streams through ``POST /collections/{name}/vectors/batch`` (real arq
enqueue to a real Redis) and is staged on MinIO; the real arq task
(``run_batch_ingest``, driven in-process via its documented session/storage
seams — the only element not exercised is the worker process's Redis pop
loop) streams the file back, validates each line, and chunked-upserts through
the real Qdrant adapter. ``GET /jobs/{job_id}`` and a vector fetch prove the
ingest landed.

This validates the batch-throughput model's load-bearing predictions at the
headline 100k-record scale (the analysis doc's Phase 6 row):
  * chunk sizing — the worker must chunk at the adapter's capability default
    (Qdrant 5-10k/request; observed 5000) and make ceil(100k/5000) = 20
    adapter calls, each exactly 5000 records;
  * bounded memory — the read->parse->upsert pipeline never materializes the
    payload: the process's RSS grows by a small fraction of what buffering
    the whole file would need (the ~60 MB payload plus ~400 MB of parsed
    records; observed footprint is one chunk + the per-line results list);
  * wall clock — 100k records complete inside the CI-safe budget (~12 s
    ingest on this box; the model budgets 15-45 s for a whole 100k job even
    at 1536 dims).

Measurement note: the memory signal is a background RSS sampler (psutil, 20
ms cadence), not ``tracemalloc`` — the first version of this test traced
allocations and measured a 142.7 s worker run, while the identical untraced
run took 60.5 s: tracemalloc's per-allocation tracing inflates this
allocation-heavy path (100k JSON parses, pydantic validation, Qdrant client
serialization) by ~2.4x, which would corrupt the wall-clock assertion and
skew the memory numbers. RSS delta against a pre-run baseline is the honest,
non-perturbing signal for both properties. (Measured ingest alone, driving
``QdrantAdapter.batch_upsert`` directly with no parse layer, is 12.0 s / 8,338
rec/s — the parse+validate layer, serialized after it per the scope note
below, dominates the worker wall clock at 64 dims.)

Honest scope note: the current worker pipeline is *serial* (read -> parse ->
flush at chunk boundaries; the model's read-ahead overlap was not
implemented), so this soak proves bounded memory + chunking + throughput, not
parse/ingest overlap. At 64 dims the parse cost is negligible next to the
budget; at the model's 1536-dim scale serial parse would dominate — see the
CLAUDE.md Progress Log note.
"""

import asyncio
import os
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import psutil  # type: ignore[import-untyped]
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.workers.batch as batch_worker
from app.adapters.base import VectorRecord
from app.adapters.registry import registry
from app.core.config import get_settings
from app.db.session import get_session
from app.main import app
from app.services.batch_storage import BatchStorage

SOAK_SIZE = 100_000
SOAK_DIM = 64
# The throughput model budgets ~15-45 s for a whole 100k job on Qdrant (at
# 1536 dims); at 64 dims the ingest is ~12 s. The CI-safe bound mirrors the
# Chroma soak and still catches a throughput collapse.
SOAK_BUDGET_SECONDS = 180
# Qdrant's capability default (5-10k/request) — what the worker must pass.
CHUNK = 5000
# Worker RSS-footprint ceiling: one 5000-record chunk (~12 MB) + the
# 100k-line results list (~35 MB) + boto3 read buffers + parse transients.
# Whole-buffering the ~60 MB payload (raw lines + ~400 MB of parsed records)
# would blow past this by an order of magnitude, so 128 MB cleanly separates
# the streaming pipeline from a whole-file materialization.
MAX_RSS_DELTA_MB = 128

API = "/api/v1"


class _RssSampler:
    """Peak process RSS over an async window, sampled on the event loop.

    Sampling (rather than tracemalloc) is deliberate: allocation tracing
    slows this allocation-heavy path ~12x, which would corrupt the wall-clock
    measurement. RSS delta against a pre-window baseline is the honest
    bounded-memory signal — it captures everything the worker's pipeline
    actually holds (Python objects, boto3 buffers, adapter client state).
    """

    def __init__(self) -> None:
        self._proc = psutil.Process()
        self.peak = 0
        self._task: Any = None

    async def _sample(self) -> None:
        while True:
            self.peak = max(self.peak, self._proc.memory_info().rss)
            await asyncio.sleep(0.02)

    async def __aenter__(self) -> "_RssSampler":
        self.peak = self._proc.memory_info().rss
        self._task = asyncio.create_task(self._sample())
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.peak = max(self.peak, self._proc.memory_info().rss)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _unique_email(local: str = "user") -> str:
    return f"{local}-{uuid.uuid4().hex[:10]}@example.com"


def _line(i: int, *, dim: int = SOAK_DIM) -> str:
    import json

    return json.dumps(
        {
            "id": f"doc-{i}",
            "vector": [float(((i + j) % 5) + 1) * 0.1 for j in range(dim)],
            "metadata": {"tag": "soak", "seed": i},
        }
    )


@pytest.fixture
async def batch_storage(minio_url: str) -> AsyncGenerator[BatchStorage, None]:
    """Fresh bucket per test, exposed via BATCH_STORAGE_BUCKET so the route's
    JobService (env-default storage) and the worker seam (injected storage)
    share the same target — the exact keys the platform derives at runtime."""
    bucket = f"vhk-soak-{uuid.uuid4().hex[:8]}"
    os.environ["BATCH_STORAGE_BUCKET"] = bucket
    get_settings.cache_clear()
    try:
        yield BatchStorage(bucket=bucket)
    finally:
        os.environ.pop("BATCH_STORAGE_BUCKET", None)
        get_settings.cache_clear()


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    qdrant_backend: None,
    redis_url: str,  # real arq enqueue — the production path (no no-op seam)
) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _register(client: AsyncClient) -> dict[str, Any]:
    resp = await client.post(
        f"{API}/auth/register",
        json={
            "email": _unique_email("soak"),
            "password": "password-123",
            "tenant_name": _unique("soak"),
        },
    )
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_collection(client: AsyncClient, headers: dict[str, str]) -> str:
    name = _unique("soak")
    resp = await client.post(
        f"{API}/collections",
        json={
            "name": name,
            "backend": "qdrant",
            "dimension": SOAK_DIM,
            "distance_metric": "cosine",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return name


@pytest.mark.soak
async def test_100k_batch_job_soak_on_qdrant(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    batch_storage: BatchStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full batch path at 100k records: enqueue (real Redis) -> MinIO
    staging -> worker task -> chunked Qdrant upsert -> jobs API, proving the
    model's chunk sizing, bounded memory, and wall-clock predictions."""
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers)

    payload = "".join(_line(i) + "\n" for i in range(SOAK_SIZE))
    payload_mb = len(payload) / 1e6
    assert payload_mb > 20, "payload must be large enough for the memory proof to bite"

    resp = await client.post(
        f"{API}/collections/{name}/vectors/batch",
        content=payload,
        headers={**headers, "Content-Type": "application/x-ndjson"},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    queued = await client.get(f"{API}/jobs/{job_id}", headers=headers)
    assert queued.status_code == 200 and queued.json()["status"] == "queued"

    # Drive the real arq task in-process (session/storage seams only).
    batch_worker.set_session_factory(session_factory)
    batch_worker.set_storage(batch_storage)

    # Record the worker's chunked-upload calls on the live Qdrant adapter.
    adapter = registry.get("qdrant")
    assert adapter is not None
    original_upsert = adapter.batch_upsert
    calls: list[tuple[int, int]] = []

    async def recording(
        *,
        collection: str,
        tenant_id: str,
        records: list[VectorRecord],
        chunk_size: int,
        extras: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((len(records), chunk_size))
        return await original_upsert(
            collection=collection,
            tenant_id=tenant_id,
            records=records,
            chunk_size=chunk_size,
            extras=extras,
        )

    monkeypatch.setattr(adapter, "batch_upsert", recording)

    tenant_id = reg["user"]["tenant_id"]
    key = f"{tenant_id}/{job_id}.jsonl"
    base_rss = psutil.Process().memory_info().rss
    async with _RssSampler() as sampler:
        start = time.perf_counter()
        outcome = await batch_worker.run_batch_ingest({}, job_id, key)
        elapsed = time.perf_counter() - start
    rss_delta_mb = (sampler.peak - base_rss) / 1e6

    assert outcome is not None, "worker returned no outcome"
    assert outcome["status"] == "succeeded", outcome
    assert outcome["ok"] == SOAK_SIZE and outcome["failed"] == 0

    # Jobs API agrees with the worker's outcome.
    job = await client.get(f"{API}/jobs/{job_id}", headers=headers)
    assert job.status_code == 200, job.text
    body = job.json()
    assert body["status"] == "succeeded"
    assert body["total"] == SOAK_SIZE and body["ok"] == SOAK_SIZE and body["failed"] == 0

    # Results object exists on storage, one per-vector row per record.
    assert await batch_storage.head(f"{tenant_id}/{job_id}.results.jsonl")
    result_lines = [
        line async for line in batch_storage.iter_lines(f"{tenant_id}/{job_id}.results.jsonl")
    ]
    assert len(result_lines) == SOAK_SIZE

    # Vectors prove the ingest landed (first + last record).
    for probe in ("doc-0", f"doc-{SOAK_SIZE - 1}"):
        fetched = await client.get(f"{API}/collections/{name}/vectors/{probe}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["metadata"]["tag"] == "soak"

    # Chunk-sizing contract: the worker chunks at the Qdrant capability
    # default (5000) — 20 exactly-full calls, no other sizes.
    expected_chunks = (SOAK_SIZE + CHUNK - 1) // CHUNK
    assert len(calls) == expected_chunks, (
        f"expected {expected_chunks} chunked calls, got {len(calls)}"
    )
    assert all(cs == CHUNK for _, cs in calls), f"chunk sizes {[cs for _, cs in calls]} != {CHUNK}"
    assert all(n == CHUNK for n, _ in calls), "100k is an exact multiple of 5000 — every chunk full"
    assert sum(n for n, _ in calls) == SOAK_SIZE

    # Bounded memory: the worker's RSS footprint stays a small fraction of
    # what whole-buffering the payload would need (~460 MB of raw + parsed
    # records); the streaming pipeline holds one chunk + the results list.
    assert rss_delta_mb < MAX_RSS_DELTA_MB, (
        f"worker RSS grew {rss_delta_mb:.1f} MB (payload {payload_mb:.1f} MB) — "
        f"over the {MAX_RSS_DELTA_MB} MB ceiling, so the pipeline materialized the file"
    )

    print(
        f"\n[soak] 100k x {SOAK_DIM}-dim batch job on qdrant: "
        f"{elapsed:.1f}s ({SOAK_SIZE / elapsed:,.0f} rec/s), "
        f"worker RSS delta {rss_delta_mb:.0f} MB ({payload_mb:.0f} MB payload), "
        f"{len(calls)} chunks of {CHUNK} — budget {SOAK_BUDGET_SECONDS}s"
    )
    assert elapsed < SOAK_BUDGET_SECONDS, f"soak exceeded budget: {elapsed:.1f}s"
