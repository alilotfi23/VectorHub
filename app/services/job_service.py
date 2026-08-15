"""Batch job orchestration (Phase 6): enqueue and status.

``create_batch_ingest`` is the enqueue path for ``POST /collections/{name}/
vectors/batch``: the tenant's concurrent-job quota is checked FIRST (at
enqueue time, per the batch note), the NDJSON body streams to object storage
at ``{tenant_id}/{job_id}.jsonl`` (never through Redis/arq), the ``jobs`` row
is created ``queued`` with the payload key, and the arq worker receives only
``{job_id, payload_key}``. ``get_job`` is the tenant-scoped status read for
``GET /api/v1/jobs/{job_id}``.

Job statuses: ``queued`` -> ``running`` -> ``succeeded`` | ``failed`` (the
worker owns the transitions; ``failed`` carries the taxonomy error_code in
``error``, e.g. ``JOB_PAYLOAD_INVALID`` or ``JOB_FAILED``).
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, ErrorCode
from app.core.security import Principal
from app.db.models import Job
from app.services.audit_service import AuditService
from app.services.batch_storage import BatchStorage, BatchStorageProtocol, payload_key
from app.services.collection_service import CollectionAccess, resolve_collection_access

# arq enqueue is injected so unit tests can record instead of touching Redis.
EnqueueFn = Callable[[str, str], Awaitable[None]]


async def enqueue_batch_job(job_id: str, payload_key_value: str) -> None:
    """Enqueue ``run_batch_ingest`` on the arq queue (the worker's Redis)."""
    from arq.connections import RedisSettings, create_pool

    url = get_settings().redis_url
    settings = RedisSettings.from_dsn(url) if url else RedisSettings()
    pool = await create_pool(settings)
    try:
        await pool.enqueue_job("run_batch_ingest", job_id, payload_key_value)
    finally:
        await pool.aclose()


# Module-level seam so API tests can record/no-op the enqueue without a live
# arq worker (the real default connects to the worker's Redis).
default_enqueue: EnqueueFn = enqueue_batch_job


class JobService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: BatchStorageProtocol | None = None,
        enqueue: EnqueueFn | None = None,
    ) -> None:
        self._session = session
        self._storage = storage or BatchStorage()
        self._enqueue = enqueue or default_enqueue
        self._audit = AuditService(session)

    @staticmethod
    def _tenant_owns(job: Job, principal: Principal) -> bool:
        return job.tenant_id == principal.tenant_id

    async def create_batch_ingest(
        self,
        actor: Principal,
        *,
        chunks: AsyncIterator[bytes],
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> Job:
        """Stage the streamed NDJSON payload, enforce the enqueue-time quota,
        create the queued job row, and hand ``{job_id, payload_key}`` to the
        worker. The payload never transits Redis/arq; the body is streamed to
        storage (never buffered whole)."""
        access = await resolve_collection_access(self._session, actor, name=name, access=access)
        from app.core.rbac import Permission, resolve_permission

        grant_role = access.actor_grant.permission if access.actor_grant else None
        if not resolve_permission(actor, Permission.VECTOR_WRITE, collection_grant=grant_role):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {Permission.VECTOR_WRITE.value}",
                status_code=403,
            )
        collection = access.collection
        tenant_id = actor.tenant_id

        # Enqueue-time quota: bounded outstanding batch work per tenant.
        limit = get_settings().max_concurrent_jobs_per_tenant
        active = (
            await self._session.execute(
                select(func.count(Job.id)).where(
                    Job.tenant_id == tenant_id,
                    Job.status.in_(("queued", "running")),
                )
            )
        ).scalar_one()
        if active >= limit:
            raise AppError(
                ErrorCode.TENANT_QUOTA_EXCEEDED,
                f"Tenant has {active} active batch jobs; limit is {limit}",
                details={"active_jobs": active, "limit": limit},
                status_code=429,
            )

        job = Job(
            tenant_id=tenant_id,
            collection_id=collection.id,
            job_type="batch_upsert",
            status="queued",
        )
        self._session.add(job)
        await self._session.flush()  # job.id populated
        key = payload_key(tenant_id, job.id)

        # Stage the payload first; only then expose the job to the worker.
        await self._storage.ensure_bucket()
        await self._storage.upload_stream(key, chunks)
        job.payload_key = key
        job.updated_at = datetime.now(UTC)

        try:
            await self._enqueue(job.id, key)
        except Exception as exc:
            job.status = "failed"
            job.error = f"JOB_FAILED: enqueue failed: {exc}"
            await self._audit.record(
                tenant_id=tenant_id,
                actor_id=actor.user_id,
                action="job.batch_upsert.enqueued",
                resource_type="job",
                resource_id=job.id,
                details={"error": str(exc)[:200]},
                result="failure",
            )
            await self._session.commit()
            raise AppError(
                ErrorCode.JOB_FAILED,
                f"Failed to enqueue batch job: {exc}",
                details={"job_id": job.id},
                status_code=503,
            ) from exc

        await self._audit.record(
            tenant_id=tenant_id,
            actor_id=actor.user_id,
            action="job.batch_upsert.enqueued",
            resource_type="job",
            resource_id=job.id,
            details={"collection": collection.name, "payload_key": key},
        )
        await self._session.commit()
        return job

    async def get_job(self, actor: Principal, job_id: str) -> Job:
        """Tenant-scoped status read. A job from another tenant (or unknown)
        is JOB_NOT_FOUND — the same no-oracle 404 the isolation suite enforces
        on every resource."""
        job = await self._session.get(Job, job_id)
        if job is None or not self._tenant_owns(job, actor):
            # Static message — echoing the queried id would make the response
            # an existence oracle (the isolation suite's no-oracle contract;
            # collections use the same discipline).
            raise AppError(ErrorCode.JOB_NOT_FOUND, "Job not found", status_code=404)
        return job
