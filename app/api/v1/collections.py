from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, require_collection_permission
from app.core.rbac import Permission
from app.core.security import Principal
from app.db.session import get_session
from app.schemas.collections import (
    CollectionPermissionListResponse,
    CollectionPermissionResponse,
    CollectionPermissionUpdateRequest,
)
from app.services.collection_service import CollectionAccess, CollectionService

# Phase 3 adds create/delete/list/get/info on this router; today it carries
# the resource-level RBAC surface: GET/PATCH /{name}/permissions,
# DELETE /{name}/permissions/{user_id}.

router = APIRouter(prefix="/collections", tags=["collections"])


@router.patch("/{name}/permissions", response_model=CollectionPermissionResponse)
async def update_collection_permissions(
    body: CollectionPermissionUpdateRequest,
    access: CollectionAccess = Depends(require_collection_permission(Permission.TENANT_MANAGE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> CollectionPermissionResponse:
    grant = await CollectionService(session).grant_permission(
        principal, access=access, user_id=body.user_id, role=body.role
    )
    return CollectionPermissionResponse(
        collection_id=grant.collection_id,
        collection_name=access.collection.name,
        user_id=grant.user_id,
        role=grant.permission,
        created_at=grant.created_at,
    )


@router.get("/{name}/permissions", response_model=CollectionPermissionListResponse)
async def list_collection_permissions(
    limit: int = Query(default=50, ge=1, le=200, description="Page size"),
    cursor: str | None = Query(
        default=None, description="Opaque keyset cursor from a previous page"
    ),
    access: CollectionAccess = Depends(require_collection_permission(Permission.TENANT_MANAGE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> CollectionPermissionListResponse:
    page = await CollectionService(session).list_permissions(
        principal, access=access, limit=limit, cursor=cursor
    )
    return CollectionPermissionListResponse(
        items=[
            CollectionPermissionResponse(
                collection_id=grant.collection_id,
                collection_name=access.collection.name,
                user_id=grant.user_id,
                role=grant.permission,
                created_at=grant.created_at,
            )
            for grant in page.items
        ],
        next_cursor=page.next_cursor,
        total=page.total,
    )


@router.delete("/{name}/permissions/{user_id}", status_code=204)
async def revoke_collection_permission(
    user_id: str,
    access: CollectionAccess = Depends(require_collection_permission(Permission.TENANT_MANAGE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await CollectionService(session).revoke_permission(principal, access=access, user_id=user_id)
    return Response(status_code=204)
