"""Collection access control (seed of the Phase 3 CollectionService).

Phase 3 builds the full collection lifecycle (create/delete/list/info) on
top of this service; today it carries what collection-scoped access needs:

- get_collection: resolve the client-facing `name` within the caller's
  tenant. A missing OR foreign collection resolves to the same
  COLLECTION_NOT_FOUND — responses must not act as an existence oracle.
- resolve_access: the single tenant-scoped resolution — collection plus the
  caller's grant on it (CollectionAccess). Routes resolve once via the
  require_collection_permission dependency and hand the access into the
  service methods, which never re-resolve. Direct service callers pass
  `name` and the methods resolve themselves; both paths run the same gate.
- check_access: resolve + gate a permission (tenant role elevated by any
  grant on the collection), returning the CollectionAccess.
- grant_permission: upsert a resource-level role for a tenant member on a
  collection (collection_permissions). Management requires TENANT_MANAGE
  (tenant admin/owner, or a grant giving that on the collection), and a
  grantee role may not exceed the granter's own effective role — an admin
  can't mint an owner.

Platform admins bypass permission checks but collection *lookup* remains
scoped to the principal's own tenant (name-based cross-tenant disambiguation
is a Phase 3+ concern).
"""

import base64
from dataclasses import dataclass

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.rbac import VALID_ROLES, Permission, effective_role, resolve_permission, role_rank
from app.core.security import Principal
from app.db.models import Collection, CollectionPermission, User
from app.services.audit_service import AuditService


@dataclass(frozen=True)
class CollectionAccess:
    """A tenant-scoped collection resolution plus the caller's grant on it
    (None when the caller has no resource-level grant).

    Routes resolve this once (via require_collection_permission) and pass it
    into service methods so the collection and the actor's grant are never
    looked up twice per request; direct service callers resolve it themselves
    from a name.
    """

    collection: Collection
    actor_grant: CollectionPermission | None


@dataclass(frozen=True)
class CollectionPermissionsPage:
    """One page of the cursor-paginated grant list."""

    items: list[CollectionPermission]
    next_cursor: str | None  # opaque keyset cursor; None = last page
    total: int


def _encode_grant_cursor(permission: str, user_id: str) -> str:
    """Opaque keyset cursor for the (role rank DESC, user_id ASC) sort."""
    raw = f"{role_rank(permission)}:{user_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_grant_cursor(cursor: str) -> tuple[int, str]:
    """Decode a cursor; malformed cursors are a client error (422)."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        rank_str, user_id = raw.split(":", 1)
        rank = int(rank_str)
    except (ValueError, UnicodeDecodeError):
        raise AppError(
            ErrorCode.VALIDATION_INVALID_CURSOR, "Invalid cursor", status_code=422
        ) from None
    if rank not in {role_rank(role) for role in VALID_ROLES} or not user_id:
        raise AppError(ErrorCode.VALIDATION_INVALID_CURSOR, "Invalid cursor", status_code=422)
    return rank, user_id


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

    async def resolve_access(self, actor: Principal, *, name: str) -> CollectionAccess:
        """The single tenant-scoped resolution: collection plus the caller's
        grant on it. Routes and service methods share this lookup."""
        collection = await self.get_collection(actor, name=name)
        grant = await self.get_permission_grant(collection.id, actor.user_id or "")
        return CollectionAccess(collection=collection, actor_grant=grant)

    async def check_access(
        self, actor: Principal, permission: Permission, *, name: str
    ) -> CollectionAccess:
        """Resolve the collection tenant-scoped and gate `permission` against
        the caller's tenant role elevated by any grant on it; raises 403/404.
        Returns the CollectionAccess so callers reuse the resolution."""
        access = await self.resolve_access(actor, name=name)
        grant_role = access.actor_grant.permission if access.actor_grant else None
        if not resolve_permission(actor, permission, collection_grant=grant_role):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {permission.value}",
                status_code=403,
            )
        return access

    async def _access_for(
        self,
        actor: Principal,
        *,
        name: str | None,
        access: CollectionAccess | None,
    ) -> CollectionAccess:
        """Reuse a pre-resolved access (route path — resolved once by the
        dependency) or resolve from a name (direct service callers). Exactly
        one must be provided; a mismatch is a programming error."""
        if (name is None) == (access is None):
            raise ValueError("Provide exactly one of `name` or `access`")
        if access is not None:
            return access
        return await self.resolve_access(actor, name=name or "")

    # --- grants ---

    async def grant_permission(
        self,
        actor: Principal,
        *,
        user_id: str,
        role: str,
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> CollectionPermission:
        access = await self._access_for(actor, name=name, access=access)
        collection, actor_grant = access.collection, access.actor_grant

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

    async def revoke_permission(
        self,
        actor: Principal,
        *,
        user_id: str,
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> None:
        """Remove a user's resource-level grant on a collection.

        Same manage gate as grant_permission (no rank guard needed — deleting
        can't escalate). Idempotent: revoking a grant that doesn't exist is a
        no-op, per REST DELETE semantics. Audited only when a row is actually
        removed.
        """
        access = await self._access_for(actor, name=name, access=access)
        collection, actor_grant = access.collection, access.actor_grant
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

    async def list_permissions(
        self,
        actor: Principal,
        *,
        limit: int = 50,
        cursor: str | None = None,
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> CollectionPermissionsPage:
        """List a collection's resource-level grants for introspection,
        cursor-paginated so large grant lists are never fully materialized.

        Same manage gate as grant/revoke: grant state is access-control state,
        and only managers should be able to enumerate who holds elevated roles
        on a collection. Tenant-scoped resolution (no existence oracle).

        Keyset pagination over the deterministic sort — role rank descending
        (owners first), then user_id. The rank CASE is derived from the
        canonical role_rank mapping; created_at is a Python-side default that
        can tie within a batch, so it must not be part of the sort key. The
        opaque cursor encodes (rank, user_id) of the last returned item, so
        pages resume exactly regardless of page size.
        """
        access = await self._access_for(actor, name=name, access=access)
        collection, actor_grant = access.collection, access.actor_grant
        grant_role = actor_grant.permission if actor_grant else None
        if not resolve_permission(actor, Permission.TENANT_MANAGE, collection_grant=grant_role):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                "Admin or owner role required to view grants",
                status_code=403,
            )

        total = await self._session.scalar(
            select(func.count())
            .select_from(CollectionPermission)
            .where(CollectionPermission.collection_id == collection.id)
        )
        assert total is not None

        rank_case = case(
            {role: role_rank(role) for role in VALID_ROLES},
            value=CollectionPermission.permission,
            else_=0,  # role_rank's default for unknown roles
        )
        stmt = (
            select(CollectionPermission)
            .where(CollectionPermission.collection_id == collection.id)
            .order_by(rank_case.desc(), CollectionPermission.user_id)
            .limit(limit + 1)  # +1 to detect whether another page exists
        )
        if cursor is not None:
            cursor_rank, cursor_user_id = _decode_grant_cursor(cursor)
            stmt = stmt.where(
                or_(
                    rank_case < cursor_rank,
                    and_(rank_case == cursor_rank, CollectionPermission.user_id > cursor_user_id),
                )
            )
        rows = list(await self._session.scalars(stmt))
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            _encode_grant_cursor(items[-1].permission, items[-1].user_id) if has_more else None
        )
        return CollectionPermissionsPage(items=items, next_cursor=next_cursor, total=total)
