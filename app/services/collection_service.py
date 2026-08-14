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

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import registry
from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import Page, paginate
from app.core.rbac import VALID_ROLES, Permission, effective_role, resolve_permission, role_rank
from app.core.security import Principal
from app.db.models import Collection, CollectionPermission, User
from app.services.audit_service import AuditService

# Per-collection backend probe bound so one hung backend can't stall a whole
# list/get response (same discipline as the /health probes).
_BACKEND_PROBE_TIMEOUT = 5.0


@dataclass(frozen=True)
class CollectionWithStatus:
    """A registry collection plus its read-path ``backend_status`` — the
    drift-visibility contract: ``exists`` (the physical object is present),
    ``missing`` (the backend deterministically reports it absent), or
    ``error`` (the probe failed — network/auth/backend; the runbook is "check
    the backend", distinct from ``missing``'s "drift")."""

    collection: Collection
    backend_status: str


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


async def resolve_collection_access(
    session: AsyncSession,
    actor: Principal,
    *,
    name: str | None = None,
    access: CollectionAccess | None = None,
) -> CollectionAccess:
    """Reuse a pre-resolved access (route path — the dependency resolved it
    once) or resolve from a name (direct service callers). Exactly one must
    be provided; a mismatch is a programming error. Module-level so
    VectorService/SearchService share this single resolution path — a route
    resolves the collection once per request and hands the ``CollectionAccess``
    into every service method (the resolve-once discipline)."""
    if (name is None) == (access is None):
        raise ValueError("Provide exactly one of `name` or `access`")
    if access is not None:
        return access
    return await CollectionService(session).resolve_access(actor, name=name or "")


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
        """Reuse a pre-resolved access (route path) or resolve from a name
        (direct service callers); delegates to the module-level helper so
        VectorService/SearchService share the single resolution path."""
        return await resolve_collection_access(self._session, actor, name=name, access=access)

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
    ) -> Page[CollectionPermission]:
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
        pages resume exactly regardless of page size. The generic keyset
        machinery lives in app.core.pagination.
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

        rank_case = case(
            {role: role_rank(role) for role in VALID_ROLES},
            value=CollectionPermission.permission,
            else_=0,  # role_rank's default for unknown roles
        )
        return await paginate(
            session=self._session,
            base=select(CollectionPermission).where(
                CollectionPermission.collection_id == collection.id
            ),
            count=select(func.count())
            .select_from(CollectionPermission)
            .where(CollectionPermission.collection_id == collection.id),
            sort_keys=[(rank_case, "desc"), (CollectionPermission.user_id, "asc")],
            limit=limit,
            cursor=cursor,
            row_key_values=lambda g: [role_rank(g.permission), g.user_id],
        )

    # --- lifecycle (Phase 3) ---

    async def create_collection(
        self,
        actor: Principal,
        *,
        name: str,
        backend: str,
        dimension: int,
        distance_metric: str,
        metadata: dict[str, Any] | None = None,
    ) -> Collection:
        """Create a collection: tenant-scoped name uniqueness, an opaque
        ``col_<uuid>`` physical name generated here (adapters never see
        client-facing names), backend object + tenant boundary provisioned
        synchronously (lazy-and-idempotent per the Tenancy Matrix), and the
        registry row. ``tenant_id`` is always the principal's — never from
        the request (isolation-suite R3/E3).

        Ordering prevents drift in the common failure cases: the registry row
        is flushed first (uniqueness enforced before any backend side effect),
        then the backend is provisioned; a backend failure rolls the row back
        so a retry is clean. Only an interrupted commit after a successful
        backend create can orphan a physical object — observable via
        ``backend_status`` and deletable, per the drift non-goal.
        """
        if not resolve_permission(actor, Permission.COLLECTION_WRITE):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {Permission.COLLECTION_WRITE.value}",
                status_code=403,
            )
        existing = await self._session.scalar(
            select(Collection.id).where(
                Collection.tenant_id == actor.tenant_id, Collection.name == name
            )
        )
        if existing is not None:
            raise AppError(
                ErrorCode.COLLECTION_ALREADY_EXISTS,
                f"Collection '{name}' already exists",
                status_code=409,
            )
        adapter = registry.get(backend)
        if adapter is None:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Backend '{backend}' is not available",
                details={"backend": backend},
                status_code=503,
            )
        collection = Collection(
            tenant_id=actor.tenant_id,
            name=name,
            backend=backend,
            dimension=dimension,
            distance_metric=distance_metric,
            physical_name=f"col_{uuid.uuid4().hex}",
            metadata_=metadata or {},
        )
        self._session.add(collection)
        try:
            await self._session.flush()  # enforces (tenant_id, name) uniqueness
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                ErrorCode.COLLECTION_ALREADY_EXISTS,
                f"Collection '{name}' already exists",
                status_code=409,
            ) from exc
        try:
            await adapter.create_collection(
                name=collection.physical_name,
                dimension=dimension,
                distance_metric=distance_metric,
            )
            await adapter.ensure_tenant(
                collection=collection.physical_name, tenant_id=actor.tenant_id
            )
        except AppError:
            await self._session.rollback()
            raise
        except Exception as exc:
            await self._session.rollback()
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Failed to provision collection '{name}' on backend '{backend}'",
                details={"backend": backend, "cause": str(exc)[:200]},
                status_code=503,
            ) from exc
        await self._audit.record(
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            action="collection.created",
            resource_type="collection",
            resource_id=collection.id,
            details={
                "name": name,
                "backend": backend,
                "dimension": dimension,
                "distance_metric": distance_metric,
            },
        )
        await self._session.commit()
        return collection

    async def delete_collection(
        self,
        actor: Principal,
        *,
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> None:
        """Hard-delete: the adapter removes the physical object from the
        backend (tolerant of an already-missing object, so a retry is clean),
        then the registry row + its grants are removed in one transaction.
        A genuine backend failure (unreachable) aborts with
        COLLECTION_BACKEND_UNAVAILABLE and the row is preserved — delete is
        destructive and immediate per the data-retention contract, and a
        failed delete must not pretend otherwise.
        """
        access = await self._access_for(actor, name=name, access=access)
        collection, actor_grant = access.collection, access.actor_grant
        grant_role = actor_grant.permission if actor_grant else None
        if not resolve_permission(actor, Permission.COLLECTION_DELETE, collection_grant=grant_role):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {Permission.COLLECTION_DELETE.value}",
                status_code=403,
            )
        adapter = registry.get(collection.backend)
        if adapter is None:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Backend '{collection.backend}' is not available",
                status_code=503,
            )
        try:
            await adapter.delete_collection(name=collection.physical_name)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Failed to delete collection on backend '{collection.backend}'",
                details={"backend": collection.backend, "cause": str(exc)[:200]},
                status_code=503,
            ) from exc
        await self._session.execute(
            delete(CollectionPermission).where(CollectionPermission.collection_id == collection.id)
        )
        await self._session.delete(collection)
        await self._audit.record(
            tenant_id=collection.tenant_id,
            actor_id=actor.user_id,
            action="collection.deleted",
            resource_type="collection",
            resource_id=collection.id,
        )
        await self._session.commit()

    async def list_collections(
        self,
        actor: Principal,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[CollectionWithStatus]:
        """Tenant-scoped, cursor-paginated listing. Every item carries its
        read-path ``backend_status`` (gathered concurrently, individually
        bounded), so drift is observable on the list surface too.
        Deterministic sort: created_at desc, id asc (created_at is a
        Python-side default that can tie within a batch, so id breaks ties).
        """
        if not resolve_permission(actor, Permission.COLLECTION_READ):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {Permission.COLLECTION_READ.value}",
                status_code=403,
            )
        base = select(Collection).where(Collection.tenant_id == actor.tenant_id)
        count = (
            select(func.count())
            .select_from(Collection)
            .where(Collection.tenant_id == actor.tenant_id)
        )
        # Sort key: floor(epoch-seconds) — the keyset cursor can only carry
        # int/str scalars, and a timestamptz column can't compare against an
        # ISO-string parameter (asyncpg types it VARCHAR). floor() must match
        # the cursor's int truncation exactly (raw extract() returns a
        # fractional double, which would drop the last page). Rows created
        # within the same second tie and are fully ordered by the id
        # tiebreaker, so the keyset stays exact; epoch seconds fit asyncpg's
        # int4 binding.
        created_at_epoch = func.floor(func.extract("epoch", Collection.created_at))
        page = await paginate(
            session=self._session,
            base=base,
            count=count,
            sort_keys=[(created_at_epoch, "desc"), (Collection.id, "asc")],
            limit=limit,
            cursor=cursor,
            row_key_values=lambda c: [int(c.created_at.timestamp()), c.id],
        )
        statuses = await self._backend_statuses(list(page.items))
        items = [
            CollectionWithStatus(collection=c, backend_status=statuses[c.id]) for c in page.items
        ]
        return Page(items=items, next_cursor=page.next_cursor, total=page.total)

    async def get_collection_with_status(
        self,
        actor: Principal,
        *,
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> CollectionWithStatus:
        """GET one collection (tenant-scoped, no existence oracle) plus its
        backend_status."""
        access = await self._access_for(actor, name=name, access=access)
        status = (await self._backend_statuses([access.collection]))[access.collection.id]
        return CollectionWithStatus(collection=access.collection, backend_status=status)

    async def update_config(
        self,
        actor: Principal,
        *,
        index_config: dict[str, Any],
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> Collection:
        """PATCH /collections/{name}/config: apply only the index parameters
        the backend's CapabilityMatrix declares hot-mutable; anything else is
        a 409 REQUIRES_REINDEX with a stated next_step (never a silent no-op).
        For Chroma the mutable subset is empty, so every request 409s.
        """
        access = await self._access_for(actor, name=name, access=access)
        collection, actor_grant = access.collection, access.actor_grant
        grant_role = actor_grant.permission if actor_grant else None
        if not resolve_permission(actor, Permission.COLLECTION_WRITE, collection_grant=grant_role):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {Permission.COLLECTION_WRITE.value}",
                status_code=403,
            )
        adapter = registry.get(collection.backend)
        if adapter is None:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Backend '{collection.backend}' is not available",
                status_code=503,
            )
        capability = getattr(adapter, "capability", None)
        mutable = set(capability().mutable_config) if callable(capability) else set()
        requested = set(index_config)
        unsupported = requested - mutable
        if unsupported:
            raise AppError(
                ErrorCode.REQUIRES_REINDEX,
                f"Index configuration changes require a reindex on backend '{collection.backend}'",
                details={
                    "next_step": f"POST /api/v1/collections/{collection.name}/reindex",
                    "requested": sorted(requested),
                    "mutable": sorted(mutable),
                },
                status_code=409,
            )
        try:
            await adapter.create_index(
                collection=collection.physical_name,
                index_config={k: index_config[k] for k in requested},
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Failed to update index config on backend '{collection.backend}'",
                details={"backend": collection.backend, "cause": str(exc)[:200]},
                status_code=503,
            ) from exc
        await self._audit.record(
            tenant_id=collection.tenant_id,
            actor_id=actor.user_id,
            action="collection.config.updated",
            resource_type="collection",
            resource_id=collection.id,
            details={"index_config": {k: index_config[k] for k in requested}},
        )
        await self._session.commit()
        return collection

    # --- backend_status probing (drift visibility, non-goal: no reconciliation) ---

    async def _backend_statuses(self, collections: list[Collection]) -> dict[str, str]:
        """Probe each collection's physical object concurrently, individually
        bounded. ``exists`` | ``missing`` | ``error`` are disjoint by
        construction: missing = the adapter deterministically reported the
        object absent; error = the probe failed (network/auth/backend), which
        points the runbook at the backend rather than at drift."""

        async def probe(collection: Collection) -> tuple[str, str]:
            adapter = registry.get(collection.backend)
            if adapter is None:
                return collection.id, "error"
            try:
                info = await asyncio.wait_for(
                    adapter.get_collection_info(name=collection.physical_name),
                    timeout=_BACKEND_PROBE_TIMEOUT,
                )
            except Exception:
                return collection.id, "error"
            return collection.id, "exists" if info is not None else "missing"

        results = await asyncio.gather(*(probe(c) for c in collections))
        return dict(results)
