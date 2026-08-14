"""Collection access control (seed of the Phase 3 CollectionService).

Phase 3 builds the full collection lifecycle (create/delete/list/info) on
top of this service; today it carries what collection-scoped access needs:

- get_collection: resolve the client-facing `name` within the caller's
  tenant. A missing OR foreign collection resolves to the same
  COLLECTION_NOT_FOUND — responses must not act as an existence oracle.
- grant_permission: upsert a resource-level role for a tenant member on a
  collection (collection_permissions). Management requires TENANT_MANAGE
  (tenant admin/owner, or a grant giving that on the collection), and a
  grantee role may not exceed the granter's own effective role — an admin
  can't mint an owner.
- check_access: the DB-backed half of resolve_permission — tenant role
  elevated by any grant on the collection.

Platform admins bypass permission checks but collection *lookup* remains
scoped to the principal's own tenant (name-based cross-tenant disambiguation
is a Phase 3+ concern).
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.rbac import VALID_ROLES, Permission, effective_role, resolve_permission, role_rank
from app.core.security import Principal
from app.db.models import Collection, CollectionPermission, User
from app.services.audit_service import AuditService


class CollectionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    # --- resolution ---

    async def get_collection(self, actor: Principal, *, name: str) -> Collection:
        collection = await self._session.scalar(
            select(Collection).where(
                Collection.tenant_id == actor.tenant_id, Collection.name == name
            )
        )
        if collection is None:
            raise AppError(ErrorCode.COLLECTION_NOT_FOUND, "Collection not found", status_code=404)
        return collection

    async def get_permission_grant(
        self, collection_id: str, user_id: str
    ) -> CollectionPermission | None:
        result = await self._session.scalar(
            select(CollectionPermission).where(
                CollectionPermission.collection_id == collection_id,
                CollectionPermission.user_id == user_id,
            )
        )
        return result

    # --- access checks (for the require_collection_permission dependency) ---

    async def check_access(
        self, actor: Principal, permission: Permission, *, name: str
    ) -> Collection:
        """Tenant-scoped resolution + permission check; raises 404/403."""
        collection = await self.get_collection(actor, name=name)
        grant = await self.get_permission_grant(collection.id, actor.user_id or "")
        if not resolve_permission(
            actor, permission, collection_grant=grant.permission if grant else None
        ):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {permission.value}",
                status_code=403,
            )
        return collection

    # --- grants ---

    async def grant_permission(
        self, actor: Principal, *, name: str, user_id: str, role: str
    ) -> CollectionPermission:
        collection = await self.get_collection(actor, name=name)
        actor_grant = await self.get_permission_grant(collection.id, actor.user_id or "")

        grant_role = actor_grant.permission if actor_grant else None
        if not resolve_permission(actor, Permission.TENANT_MANAGE, collection_grant=grant_role):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                "Admin or owner role required to manage grants",
                status_code=403,
            )
        if role not in VALID_ROLES:
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS, f"Invalid role: {role}", status_code=422
            )

        # The grantee must be a member of the collection's tenant.
        grantee = await self._session.scalar(
            select(User).where(User.id == user_id, User.tenant_id == collection.tenant_id)
        )
        if grantee is None:
            raise AppError(ErrorCode.TENANT_MEMBER_NOT_FOUND, "Member not found", status_code=404)

        # An admin can't mint an owner: granted role <= granter's effective role.
        if not actor.is_platform_admin:
            effective = effective_role(actor.role, grant_role)
            if role_rank(role) > role_rank(effective):
                raise AppError(
                    ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                    "Cannot grant a role above your own",
                    status_code=403,
                )

        row_id = await self._session.scalar(
            pg_insert(CollectionPermission)
            .values(collection_id=collection.id, user_id=user_id, permission=role)
            .on_conflict_do_update(
                index_elements=[CollectionPermission.collection_id, CollectionPermission.user_id],
                set_={"permission": role},
            )
            .returning(CollectionPermission.id)
        )
        assert row_id is not None
        await self._audit.record(
            tenant_id=collection.tenant_id,
            actor_id=actor.user_id,
            action="collection.permission.granted",
            resource_type="collection",
            resource_id=collection.id,
            details={"user_id": user_id, "role": role},
        )
        await self._session.commit()
        row = await self._session.get(CollectionPermission, row_id)
        assert row is not None
        # The upsert bypassed the unit of work, so the identity-mapped object
        # may hold the pre-update role — reload before returning.
        await self._session.refresh(row)
        return row

    async def revoke_permission(self, actor: Principal, *, name: str, user_id: str) -> None:
        """Remove a user's resource-level grant on a collection.

        Same manage gate as grant_permission (no rank guard needed — deleting
        can't escalate). Idempotent: revoking a grant that doesn't exist is a
        no-op, per REST DELETE semantics. Audited only when a row is actually
        removed.
        """
        collection = await self.get_collection(actor, name=name)
        actor_grant = await self.get_permission_grant(collection.id, actor.user_id or "")
        grant_role = actor_grant.permission if actor_grant else None
        if not resolve_permission(actor, Permission.TENANT_MANAGE, collection_grant=grant_role):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                "Admin or owner role required to manage grants",
                status_code=403,
            )
        grant = await self.get_permission_grant(collection.id, user_id)
        if grant is None:
            return
        await self._session.delete(grant)
        await self._audit.record(
            tenant_id=collection.tenant_id,
            actor_id=actor.user_id,
            action="collection.permission.revoked",
            resource_type="collection",
            resource_id=collection.id,
            details={"user_id": user_id},
        )
        await self._session.commit()
