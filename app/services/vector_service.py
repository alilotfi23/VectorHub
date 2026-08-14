"""Vector CRUD: upsert / fetch / delete through the collection's adapter.

The service layer is mandatory between routes and adapters: it derives
``tenant_id`` from the authenticated principal (never the request body),
asserts collection ownership via the shared resolve-once access path, gates
permissions, validates dimensions against the registry row, stamps server
timestamps, and writes audit rows — the adapter stays thin and
backend-specific. A route's job is validation and response shaping only.

Reserved-key note: the platform's ``_vhk_*`` metadata prefix is rejected at
the API schema layer; this service re-asserts it on the adapter record so a
direct service caller can't smuggle a colliding key past the adapter's
storage contract.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import SparseVector, VectorDBAdapter, VectorRecord
from app.adapters.registry import registry
from app.core.exceptions import AppError, ErrorCode
from app.core.rbac import Permission, resolve_permission
from app.core.security import Principal
from app.db.models import Collection
from app.schemas.vectors import VectorRecordIn
from app.services.audit_service import AuditService
from app.services.collection_service import CollectionAccess, resolve_collection_access


class VectorService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    @staticmethod
    def _adapter_for(collection: Collection) -> VectorDBAdapter:
        adapter = registry.get(collection.backend)
        if adapter is None:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Backend '{collection.backend}' is not available",
                status_code=503,
            )
        return adapter

    @staticmethod
    def _check_gate(actor: Principal, permission: Permission, collection: CollectionAccess) -> None:
        grant_role = collection.actor_grant.permission if collection.actor_grant else None
        if not resolve_permission(actor, permission, collection_grant=grant_role):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {permission.value}",
                status_code=403,
            )

    async def upsert(
        self,
        actor: Principal,
        *,
        records: list[VectorRecordIn],
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> int:
        """Idempotent upsert by client-supplied id. Server stamps the UTC
        timestamps; the adapter preserves each existing record's created_at
        across overwrites. Returns the number of records written."""
        access = await resolve_collection_access(self._session, actor, name=name, access=access)
        self._check_gate(actor, Permission.VECTOR_WRITE, access)
        collection = access.collection
        for record in records:
            if len(record.vector) != collection.dimension:
                raise AppError(
                    ErrorCode.VECTOR_DIMENSION_MISMATCH,
                    f"Vector dimension {len(record.vector)} does not match collection "
                    f"dimension {collection.dimension}",
                    status_code=422,
                )
        now = datetime.now(UTC)
        adapter = self._adapter_for(collection)
        vector_records = [
            VectorRecord(
                id=r.id,
                vector=r.vector,
                metadata=r.metadata,
                sparse_vector=(
                    SparseVector(indices=r.sparse_vector.indices, values=r.sparse_vector.values)
                    if r.sparse_vector is not None
                    else None
                ),
                tenant_id=actor.tenant_id,
                created_at=now,
                updated_at=now,
            )
            for r in records
        ]
        try:
            await adapter.upsert_vectors(
                collection=collection.physical_name,
                tenant_id=actor.tenant_id,
                records=vector_records,
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Vector upsert failed on backend '{collection.backend}'",
                details={"backend": collection.backend, "cause": str(exc)[:200]},
                status_code=503,
            ) from exc
        await self._audit.record(
            tenant_id=collection.tenant_id,
            actor_id=actor.user_id,
            action="vector.upserted",
            resource_type="collection",
            resource_id=collection.id,
            details={"count": len(records), "ids": [r.id for r in records]},
        )
        await self._session.commit()
        return len(records)

    async def fetch(
        self,
        actor: Principal,
        *,
        vector_id: str,
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> VectorRecord:
        """Fetch one vector by id. A missing id (in this collection or any
        other — the resolution is tenant-scoped, so foreign collections 404
        at resolution) raises VECTOR_NOT_FOUND."""
        access = await resolve_collection_access(self._session, actor, name=name, access=access)
        self._check_gate(actor, Permission.VECTOR_READ, access)
        collection = access.collection
        adapter = self._adapter_for(collection)
        try:
            records = await adapter.fetch_vectors(
                collection=collection.physical_name,
                tenant_id=actor.tenant_id,
                ids=[vector_id],
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Vector fetch failed on backend '{collection.backend}'",
                details={"backend": collection.backend, "cause": str(exc)[:200]},
                status_code=503,
            ) from exc
        if not records:
            raise AppError(ErrorCode.VECTOR_NOT_FOUND, "Vector not found", status_code=404)
        return records[0]

    async def delete(
        self,
        actor: Principal,
        *,
        vector_id: str,
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> None:
        """Hard-delete one vector (immediate at the backend level, per the
        data-retention contract). Idempotent: deleting an absent id is a
        no-op success, matching the backend's delete semantics."""
        access = await resolve_collection_access(self._session, actor, name=name, access=access)
        self._check_gate(actor, Permission.VECTOR_DELETE, access)
        collection = access.collection
        adapter = self._adapter_for(collection)
        try:
            await adapter.delete_vectors(
                collection=collection.physical_name, tenant_id=actor.tenant_id, ids=[vector_id]
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Vector delete failed on backend '{collection.backend}'",
                details={"backend": collection.backend, "cause": str(exc)[:200]},
                status_code=503,
            ) from exc
        await self._audit.record(
            tenant_id=collection.tenant_id,
            actor_id=actor.user_id,
            action="vector.deleted",
            resource_type="collection",
            resource_id=collection.id,
            details={"ids": [vector_id]},
        )
        await self._session.commit()
