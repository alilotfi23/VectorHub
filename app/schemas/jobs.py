"""Job status schemas (Phase 6)."""

from datetime import datetime

from pydantic import BaseModel, Field


class JobOut(BaseModel):
    """Status/counts of one batch job (``GET /api/v1/jobs/{job_id}``)."""

    id: str
    job_type: str
    status: str = Field(description="queued | running | succeeded | failed")
    total: int
    ok: int
    failed: int
    error: str | None = None
    created_at: datetime
    updated_at: datetime
