"""add global administrator identity flag

Revision ID: 7c8d9e0f1a2b
Revises: f5b9059aae3f
Create Date: 2026-09-04 14:35:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "7c8d9e0f1a2b"
down_revision: str | Sequence[str] | None = "f5b9059aae3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_global_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_users_is_global_admin", "users", ["is_global_admin"], unique=False)
    # SQLite cannot ALTER COLUMN to drop a default.  Keeping the harmless
    # migration-time default there is equivalent for old rows and the ORM
    # still treats the field as a normal boolean; PostgreSQL gets the clean
    # schema without a server default.
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("users", "is_global_admin", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_users_is_global_admin", table_name="users")
    op.drop_column("users", "is_global_admin")
