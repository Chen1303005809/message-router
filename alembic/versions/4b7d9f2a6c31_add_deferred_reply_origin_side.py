"""record the side that owns a delayed passive-reply callback

Revision ID: 4b7d9f2a6c31
Revises: a6f3c8d1e947, d4e6a81c9b20
Create Date: 2026-09-21 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4b7d9f2a6c31"
down_revision: str | Sequence[str] | None = ("a6f3c8d1e947", "d4e6a81c9b20")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deferred_passive_replies",
        sa.Column("origin_side", sa.String(length=16), nullable=True),
    )
    op.create_index(
        "ix_deferred_passive_replies_side_expiry",
        "deferred_passive_replies",
        ["case_ref", "origin_side", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_deferred_passive_replies_side_expiry",
        table_name="deferred_passive_replies",
    )
    op.drop_column("deferred_passive_replies", "origin_side")
