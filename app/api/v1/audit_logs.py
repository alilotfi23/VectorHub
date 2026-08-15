"""Audit-log read route (Phase 6) — ``GET /api/v1/admin/audit-logs``.

Tenant-scoped, cursor-paginated view of the append-only ``audit_log`` table,
gated on ``tenant:manage`` (admin/owner). The table is INSERT-only by design
(DB grants + all-role guard trigger); this route only ever SELECTs.
"""

from typing import Any, cast

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.pagination import Page, paginate
from app.core.rbac import Permission
from app.core.security import Principal
from app.db.models import AuditLog
from app.db.session import get_session
from app.schemas.auth import StrictRequest

router = APIRouter(prefix="/admin", tags=["admin"])

MAX_LIMIT = 200


class AuditLogOut(StrictRequest):
    id: str
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    details: dict[str, Any]
    result: str
    created_at: str


class AuditLogPage(StrictRequest):
    items: list[AuditLogOut]
    next_cursor: str | None
    total: int


@router.get("/audit-logs", response_model=AuditLogPage)
async def list_audit_logs(
    limit: int = 50,
    cursor: str | None = None,
    principal: Principal = Depends(require_permission(Permission.TENANT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> AuditLogPage:
    """Tenant's audit records, newest first (admin/owner only). `limit` 1–200,
    opaque `cursor` for the next page."""
    limit = max(1, min(limit, MAX_LIMIT))
    base = select(AuditLog).where(AuditLog.tenant_id == principal.tenant_id)
    count = select(func.count(AuditLog.id)).where(AuditLog.tenant_id == principal.tenant_id)
    page: Page[AuditLog] = await paginate(
        session,
        base=base,
        count=count,
        sort_keys=[(AuditLog.created_at, "desc"), (AuditLog.id, "desc")],
        limit=limit,
        cursor=cursor,
        row_key_values=lambda row: [
            cast(int, row.created_at.timestamp()),
            row.id,
        ],
    )
    return AuditLogPage(
        items=[
            AuditLogOut(
                id=row.id,
                actor_id=row.actor_id,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                details=row.details,
                result=row.result,
                created_at=row.created_at.isoformat(),
            )
            for row in page.items
        ],
        next_cursor=page.next_cursor,
        total=page.total,
    )
