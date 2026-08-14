from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.auth import MemberRole

# Phase 3 adds the full collection lifecycle schemas here (create/update/
# info responses); this module currently carries what resource-level access
# control needs.


class CollectionPermissionUpdateRequest(BaseModel):
    # extra="forbid": the envelope deliberately has no tenant_id field — a
    # client-supplied tenant/owner id must be rejected, never silently dropped
    # (isolation-suite R3/E3).
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, description="Tenant member to grant the role to")
    role: MemberRole


class CollectionPermissionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    collection_id: str
    collection_name: str
    user_id: str
    role: str
    created_at: datetime
