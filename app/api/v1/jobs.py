"""Job status routes (Phase 6).

``GET /api/v1/jobs/{job_id}`` is the tenant-scoped read for async batch jobs
(enqueued via ``POST /collections/{name}/vectors/batch``). The job id is a
client-visible handle only within its tenant: a job belonging to another
tenant (or unknown) is ``JOB_NOT_FOUND`` — the same no-oracle 404 the
isolation suite enforces on every resource, so a response can never be used
as an existence oracle.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal
from app.core.security import Principal
from app.db.session import get_session
from app.schemas.jobs import JobOut
from app.services.job_service import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> JobOut:
    """Status/counts of one batch job (tenant-scoped)."""
    job = await JobService(session).get_job(principal, job_id)
    return JobOut(
        id=job.id,
        job_type=job.job_type,
        status=job.status,
        total=job.total,
        ok=job.ok,
        failed=job.failed,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
