"""store each member's intelligent-bot open_userid binding

Revision ID: 9a4e7c2d1f60
Revises: e61a3d7f2b90
Create Date: 2026-10-10 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9a4e7c2d1f60"
down_revision: str | Sequence[str] | None = "e61a3d7f2b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("wecom_open_userid", sa.String(length=128), nullable=True))
    op.create_index(
        "ix_users_wecom_open_userid", "users", ["wecom_open_userid"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_users_wecom_open_userid", table_name="users")
    op.drop_column("users", "wecom_open_userid")
