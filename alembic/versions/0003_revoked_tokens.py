"""add access-token jti deny-list (revoked_tokens)

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "revoked_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("jti", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
    )
    # unique+index on the model folds into a unique index named ix_*.
    op.create_index("ix_revoked_tokens_jti", "revoked_tokens", ["jti"], unique=True)
    # App CRUD comes via the default privileges set in 0001; grant explicitly
    # too so the runtime role never depends on a later ALTER DEFAULT PRIVILEGES
    # change. No audit-log restriction here — this table is ephemeral.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE revoked_tokens TO app")


def downgrade() -> None:
    op.drop_table("revoked_tokens")
