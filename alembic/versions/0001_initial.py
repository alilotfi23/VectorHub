"""initial control-plane schema, two-role provisioning, audit_log guard trigger

Revision ID: 0001
Revises:
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="viewer"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_hash", sa.String(255), nullable=False),
        sa.Column("prefix", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rate_limit_qps", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_table(
        "collections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("backend", sa.String(32), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("distance_metric", sa.String(32), nullable=False),
        sa.Column("physical_name", sa.String(64), nullable=False, unique=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_collections_tenant_name"),
    )
    op.create_index("ix_collections_tenant_id", "collections", ["tenant_id"])
    op.create_table(
        "collection_permissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("collection_id", sa.String(36), sa.ForeignKey("collections.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("permission", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("collection_id", "user_id", name="uq_coll_perm_collection_user"),
    )
    op.create_index(
        "ix_collection_permissions_collection_id", "collection_permissions", ["collection_id"]
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("collection_id", sa.String(36), sa.ForeignKey("collections.id"), nullable=True),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("payload_key", sa.String(255), nullable=True),
        sa.Column("results_key", sa.String(255), nullable=True),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    # --- Two-role provisioning (idempotent; passwords set by deployment) ---
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'app') THEN
                CREATE ROLE app LOGIN;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'migrator') THEN
                CREATE ROLE migrator LOGIN;
            END IF;
        END $$;
        """
    )
    # Runtime app role: normal CRUD on control-plane tables...
    op.execute("GRANT USAGE ON SCHEMA public TO app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app")
    # ...and future tables via default privileges (audit_log is re-restricted below).
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app"
    )
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app")
    # ...except audit_log: INSERT/SELECT only (no UPDATE/DELETE grants).
    op.execute("REVOKE ALL PRIVILEGES ON TABLE audit_log FROM app")
    op.execute("GRANT SELECT, INSERT ON TABLE audit_log TO app")
    # migrator holds DDL and full table rights — in prod it owns the tables it
    # creates, so full grants mirror that reality. The guard trigger below is
    # what makes it safe to grant audit_log rights to the DDL role.
    op.execute("GRANT CREATE ON SCHEMA public TO migrator")
    op.execute("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO migrator")

    # --- audit_log guard trigger: raises on any UPDATE/DELETE, ALL roles ---
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_guard() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: UPDATE/DELETE forbidden by design';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # One statement per op.execute: asyncpg rejects multi-command prepared statements.
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
    op.execute(
        "CREATE TRIGGER audit_log_no_update_delete "
        "BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_guard()")
    op.drop_table("jobs")
    op.drop_table("audit_log")
    op.drop_table("collection_permissions")
    op.drop_table("collections")
    op.drop_table("refresh_tokens")
    op.drop_table("api_keys")
    op.drop_table("users")
    op.drop_table("tenants")
    # Roles are intentionally left in place (creation is idempotent).
