"""Similarity search through the collection's adapter.

Query is a read path, so no audit row (the audit contract covers writes).
Filters arrive already shape-validated by the API schema (chroma-shaped
normalized subset); backend-specific semantic rejections surface as
VALIDATION_INVALID_FILTER from the adapter. Score semantics are backend-native
(see the adapter docstring) — this service passes them through untouched.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import QueryResult, SparseVector
from app.adapters.registry import registry
from app.core.exceptions import AppError, ErrorCode
from app.core.rbac import Permission, resolve_permission
from app.core.security import Principal
from app.services.collection_service import CollectionAccess, resolve_collection_access


class SearchService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def query(
        self,
        actor: Principal,
        *,
        vector: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> list[QueryResult]:
        """Top-k similarity search with optional metadata filters. The
        collection is resolved tenant-scoped (no existence oracle) and the
        caller's VECTOR_READ is gated with any resource-level grant applied."""
        access = await resolve_collection_access(self._session, actor, name=name, access=access)
        grant_role = access.actor_grant.permission if access.actor_grant else None
        if not resolve_permission(actor, Permission.VECTOR_READ, collection_grant=grant_role):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {Permission.VECTOR_READ.value}",
                status_code=403,
            )
        collection = access.collection
        if len(vector) != collection.dimension:
            raise AppError(
                ErrorCode.VECTOR_DIMENSION_MISMATCH,
                f"Query vector dimension {len(vector)} does not match collection "
                f"dimension {collection.dimension}",
                status_code=422,
            )
        adapter = registry.get(collection.backend)
        if adapter is None:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Backend '{collection.backend}' is not available",
                status_code=503,
            )
        try:
            return await adapter.query(
                collection=collection.physical_name,
                tenant_id=actor.tenant_id,
                vector=vector,
                top_k=top_k,
                filters=filters,
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Query failed on backend '{collection.backend}'",
                details={"backend": collection.backend, "cause": str(exc)[:200]},
                status_code=503,
            ) from exc

    async def hybrid(
        self,
        actor: Principal,
        *,
        vector: list[float],
        sparse_vector: SparseVector | None,
        query_text: str | None,
        alpha: float,
        top_k: int,
        filters: dict[str, Any] | None = None,
        name: str | None = None,
        access: CollectionAccess | None = None,
    ) -> list[QueryResult]:
        """Hybrid dense+keyword search per CLAUDE.md's hybrid contract. The
        backend's CapabilityMatrix row decides which input is required:
        Qdrant/Milvus (``sparse+vector``) need ``sparse_vector`` (else 422
        VECTOR_SPARSE_REQUIRED); Weaviate (``text+vector``) needs
        ``query_text`` (else 422 VALIDATION_GENERIC); Chroma raises
        VALIDATION_UNSUPPORTED_OPERATION from the adapter. ``alpha`` is
        translated per backend (Weaviate native; Qdrant prefetch weights +
        RRF fusion)."""
        access = await resolve_collection_access(self._session, actor, name=name, access=access)
        grant_role = access.actor_grant.permission if access.actor_grant else None
        if not resolve_permission(actor, Permission.VECTOR_READ, collection_grant=grant_role):
            raise AppError(
                ErrorCode.AUTH_INSUFFICIENT_SCOPE,
                f"Requires permission: {Permission.VECTOR_READ.value}",
                status_code=403,
            )
        collection = access.collection
        if len(vector) != collection.dimension:
            raise AppError(
                ErrorCode.VECTOR_DIMENSION_MISMATCH,
                f"Query vector dimension {len(vector)} does not match collection "
                f"dimension {collection.dimension}",
                status_code=422,
            )
        adapter = registry.get(collection.backend)
        if adapter is None:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Backend '{collection.backend}' is not available",
                status_code=503,
            )
        capability = getattr(adapter, "capability", None)
        mode = capability().hybrid_mode if callable(capability) else None
        if mode == "sparse+vector" and sparse_vector is None:
            raise AppError(
                ErrorCode.VECTOR_SPARSE_REQUIRED,
                "Hybrid search on this backend requires a sparse_vector",
                details={"backend": collection.backend, "capability": "sparse_vector"},
                status_code=422,
            )
        if mode == "text+vector" and not query_text:
            raise AppError(
                ErrorCode.VALIDATION_GENERIC,
                "Hybrid search on this backend requires query_text",
                details={"backend": collection.backend, "capability": "query_text"},
                status_code=422,
            )
        try:
            return await adapter.hybrid_search(
                collection=collection.physical_name,
                tenant_id=actor.tenant_id,
                vector=vector,
                sparse_vector=sparse_vector,
                query_text=query_text,
                alpha=alpha,
                top_k=top_k,
                filters=filters,
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Hybrid query failed on backend '{collection.backend}'",
                details={"backend": collection.backend, "cause": str(exc)[:200]},
                status_code=503,
            ) from exc
