from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, require_collection_permission
from app.core.rbac import Permission
from app.core.security import Principal
from app.db.models import Collection
from app.db.session import get_session
from app.schemas.collections import (
    CollectionPermissionResponse,
    CollectionPermissionUpdateRequest,
)
from app.services.collection_service import CollectionService

# Phase 3 adds create/delete/list/get/info on this router; today it carries
# the resource-level RBAC surface: GET/PATCH /{name}/permissions,
# DELETE /{name}/permissions/{user_id}.

router = APIRouter(prefix="/collections", tags=["collections"])


@router.patch("/{name}/permissions", response_model=CollectionPermissionResponse)
async def update_collection_permissions(
    name: str,
    body: CollectionPermissionUpdateRequest,
    collection: Collection = Depends(require_collection_permission(Permission.TENANT_MANAGE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> CollectionPermissionResponse:
    grant = await CollectionService(session).grant_permission(
        principal, name=name, user_id=body.user_id, role=body.role
    )
    return CollectionPermissionResponse(
        collection_id=grant.collection_id,
        collection_name=collection.name,
        user_id=grant.user_id,
        role=grant.permission,
        created_at=grant.created_at,
    )


@router.get("/{name}/permissions", response_model=list[CollectionPermissionResponse])
async def list_collection_permissions(
    name: str,
    collection: Collection = Depends(require_collection_permission(Permission.TENANT_MANAGE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> list[CollectionPermissionResponse]:
    grants = await CollectionService(session).list_permissions(principal, name=name)
    return [
        CollectionPermissionResponse(
            collection_id=grant.collection_id,
            collection_name=collection.name,
            user_id=grant.user_id,
            role=grant.permission,
            created_at=grant.created_at,
        )
        for grant in grants
    ]


@router.delete("/{name}/permissions/{user_id}", status_code=204)
async def revoke_collection_permission(
    name: str,
    user_id: str,
    collection: Collection = Depends(require_collection_permission(Permission.TENANT_MANAGE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await CollectionService(session).revoke_permission(principal, name=name, user_id=user_id)
    return Response(status_code=204)
