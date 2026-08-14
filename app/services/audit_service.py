"""Append-only audit logging for write operations.

audit_log is INSERT-only by design (DB grants + all-role guard trigger, see
the initial migration); this service never updates or deletes rows. Phase 2
audits the writes it introduces (tenant creation, API key lifecycle); the
request-level audit middleware lands in Phase 6 on top of the same table.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        tenant_id: str,
        actor_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
        result: str = "success",
    ) -> None:
        """Stage an audit row in the current transaction (caller commits).

        Failures are not audited here: services raise before reaching this
        call, and the request-level middleware (Phase 6) will own failure
        records.
        """
        self._session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details or {},
                result=result,
            )
        )
