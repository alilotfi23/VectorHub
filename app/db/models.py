import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="viewer")  # owner|admin|editor|viewer
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    key_hash: Mapped[str] = mapped_column(String(255))  # hashed at rest
    prefix: Mapped[str] = mapped_column(String(32))  # display-only
    role: Mapped[str] = mapped_column(String(32), default="editor")  # owner|admin|editor|viewer
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_limit_qps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RevokedToken(Base):
    """jti deny-list for stateless access tokens.

    Entries are only meaningful while the token could still be presented
    (within jwt_access_ttl_minutes of issuance), so stale rows are purged
    opportunistically on each logout; a stale row is harmless anyway — an
    expired token is already rejected at decode. Postgres is the source of
    truth; Phase 6's Redis cache fronts this table.
    """

    __tablename__ = "revoked_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_collections_tenant_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))  # client-facing, per-tenant
    backend: Mapped[str] = mapped_column(String(32))  # weaviate|qdrant|milvus|chroma
    dimension: Mapped[int] = mapped_column(Integer)
    distance_metric: Mapped[str] = mapped_column(String(32))
    physical_name: Mapped[str] = mapped_column(String(64), unique=True)  # opaque col_<uuid>
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CollectionPermission(Base):
    __tablename__ = "collection_permissions"
    __table_args__ = (
        UniqueConstraint("collection_id", "user_id", name="uq_coll_perm_collection_user"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    collection_id: Mapped[str] = mapped_column(String(36), ForeignKey("collections.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    permission: Mapped[str] = mapped_column(String(32))  # resource-level grant
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditLog(Base):
    """Append-only. No UPDATE/DELETE — enforced by DB grants and a guard trigger
    (see the initial Alembic migration)."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)
    collection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("collections.id"), nullable=True
    )
    job_type: Mapped[str] = mapped_column(String(32))  # batch_upsert|batch_delete|...
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    payload_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    results_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
