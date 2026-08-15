"""The async batch-ingest worker task (Phase 6).

``run_batch_ingest(ctx, job_id, payload_key)`` is the arq-side of the batch
data path: it receives only ``{job_id, payload_key}`` (the payload never
transits Redis/arq), streams the JSONL back from object storage in bounded
chunks, validates each line against the same strict record schema the sync
path uses, and upserts in per-backend-sized chunks through the adapter's
``batch_upsert`` (the chunking contract from the throughput model: Chroma
100–1k, Qdrant 5–10k, Weaviate ~1k, Milvus 1–10k — taken from the adapter's
CapabilityEntry, never assumed). Per-vector outcomes stream to
``{tenant_id}/{job_id}.results.jsonl``; ``GET /jobs/{job_id}`` reads the
status/counts off the jobs row.

Failure semantics (per CLAUDE.md): a malformed line is a *per-vector*
outcome (counted ``failed``, recorded in the results object), while a payload
with zero valid lines is a whole-file validation failure ->
``JOB_PAYLOAD_INVALID`` (job ``failed`` with that code in ``error``). A
backend failure mid-ingest aborts the job (``JOB_FAILED``); retry is safe
because upserts are idempotent. Dimension mismatches are per-vector outcomes,
checked here against the registry row (the sync path checks them too).

Test seams: this module's session factory and storage are module-level and
overridable (``set_session_factory`` / ``set_storage``) so integration tests
drive the real task against the test Postgres + MinIO without a live arq
worker process.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.base import VectorRecord
from app.adapters.registry import registry
from app.core.exceptions import ErrorCode
from app.db.models import Collection, Job
from app.schemas.vectors import VectorRecordIn
from app.services.audit_service import AuditService
from app.services.batch_storage import BatchStorage, results_key

_session_factory: async_sessionmaker[AsyncSession] | None = None
_storage: BatchStorage | None = None


def set_session_factory(factory: async_sessionmaker[AsyncSession]) -> None:
    """Test seam: point the worker's DB access at the test Postgres."""
    global _session_factory
    _session_factory = factory


def set_storage(storage: BatchStorage) -> None:
    """Test seam: point the worker's object storage at the test MinIO."""
    global _storage
    _storage = storage


def _get_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        from app.db.session import SessionLocal

        _session_factory = SessionLocal
    return _session_factory


def _get_storage() -> BatchStorage:
    global _storage
    if _storage is None:
        _storage = BatchStorage()
    return _storage


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def run_batch_ingest(
    ctx: dict[str, Any], job_id: str, payload_key_value: str
) -> dict[str, Any] | None:
    """Stream the staged payload, validate per line, chunked-upsert through the
    collection's adapter, stream per-vector outcomes, and drive the job row."""
    del ctx  # arq context not needed here (storage/DB come from the seams)
    factory = _get_factory()
    async with factory() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return None  # row gone — nothing to do
        if job.status in ("running", "succeeded"):
            return None  # already processed (idempotent retry guard)
        job.status = "running"
        job.updated_at = _utcnow()
        await session.commit()

        try:
            outcome = await _run_job(session, job, payload_key_value)
        except Exception as exc:
            job.status = "failed"
            job.error = f"{ErrorCode.JOB_FAILED.value}: {exc}"
            job.updated_at = _utcnow()
            await AuditService(session).record(
                tenant_id=job.tenant_id,
                actor_id=None,
                action="job.batch_upsert.completed",
                resource_type="job",
                resource_id=job.id,
                details={"error": str(exc)[:200]},
                result="failure",
            )
            await session.commit()
            return {"job_id": job_id, "status": "failed", "error": job.error}

        job.total = outcome["total"]
        job.ok = outcome["ok"]
        job.failed = outcome["failed"]
        job.status = outcome["status"]
        job.error = outcome.get("error")
        job.results_key = outcome["results_key"]
        job.updated_at = _utcnow()
        await AuditService(session).record(
            tenant_id=job.tenant_id,
            actor_id=None,
            action="job.batch_upsert.completed",
            resource_type="job",
            resource_id=job.id,
            details={
                "total": outcome["total"],
                "ok": outcome["ok"],
                "failed": outcome["failed"],
            },
            result="success" if outcome["status"] == "succeeded" else "failure",
        )
        await session.commit()
        return {
            "job_id": job_id,
            "status": job.status,
            "total": job.total,
            "ok": job.ok,
            "failed": job.failed,
            **({"error": job.error} if job.error is not None else {}),
        }


async def _run_job(session: AsyncSession, job: Job, payload_key_value: str) -> dict[str, Any]:
    collection = await session.get(Collection, job.collection_id) if job.collection_id else None
    if collection is None:
        raise RuntimeError("collection row missing for job")
    adapter = registry.get(collection.backend)
    if adapter is None:
        raise RuntimeError(f"backend '{collection.backend}' not registered")
    chunk_size = int(adapter.capability().default_batch_chunk_size)
    storage = _get_storage()

    chunk: list[VectorRecord] = []
    total = 0
    ok = 0
    results: list[dict[str, Any]] = []

    async def flush() -> None:
        nonlocal ok
        if not chunk:
            return
        await adapter.batch_upsert(
            collection=collection.physical_name,
            tenant_id=job.tenant_id,
            records=chunk,
            chunk_size=chunk_size,
        )
        ok += len(chunk)
        chunk.clear()

    async for line in storage.iter_lines(payload_key_value):
        total += 1
        record_id = f"line-{total}"
        error: str | None = None
        try:
            data = json.loads(line)
            parsed = VectorRecordIn.model_validate(data)
            if len(parsed.vector) != collection.dimension:
                raise ValueError(
                    f"vector dimension {len(parsed.vector)} does not match "
                    f"collection dimension {collection.dimension}"
                )
            record_id = parsed.id
            now = _utcnow()
            chunk.append(
                VectorRecord(
                    id=parsed.id,
                    vector=parsed.vector,
                    metadata=parsed.metadata,
                    sparse_vector=parsed.sparse_vector,
                    tenant_id=job.tenant_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            if len(chunk) >= chunk_size:
                await flush()
        except Exception as exc:  # per-vector outcome, not a whole-file failure
            error = str(exc)[:300]
            results.append({"id": record_id, "ok": False, "error": error})
        if error is None:
            results.append({"id": record_id, "ok": True})

    await flush()

    # Whole-file validation failure: zero valid lines -> JOB_PAYLOAD_INVALID.
    if ok == 0 and total > 0:
        results_key_value = await _write_results(storage, job, results)
        return {
            "total": total,
            "ok": 0,
            "failed": total,
            "status": "failed",
            "error": f"{ErrorCode.JOB_PAYLOAD_INVALID.value}: no valid records in payload",
            "results_key": results_key_value,
        }
    results_key_value = await _write_results(storage, job, results)
    return {
        "total": total,
        "ok": ok,
        "failed": total - ok,
        "status": "succeeded",
        "results_key": results_key_value,
    }


async def _write_results(storage: BatchStorage, job: Job, results: list[dict[str, Any]]) -> str:
    """Per-vector outcomes to ``{tenant_id}/{job_id}.results.jsonl``. The
    results object is a diagnostics artifact; it is buffered in memory (small
    JSON lines, bounded by the record count, not the payload bytes) and
    written once on completion."""
    key = results_key(job.tenant_id, job.id)

    async def chunks() -> AsyncIterator[bytes]:
        for row in results:
            yield (json.dumps(row) + "\n").encode()

    await storage.ensure_bucket()
    await storage.upload_stream(key, chunks())
    return key
