"""persist delayed passive-reply callback contexts

Revision ID: c9e4b7a1d2f3
Revises: 7c8d9e0f1a2b
Create Date: 2026-09-07 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "c9e4b7a1d2f3"
down_revision: str | Sequence[str] | None = "7c8d9e0f1a2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deferred_passive_replies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_ref", sa.String(length=32), nullable=True),
        sa.Column("req_id", sa.String(length=256), nullable=False),
        sa.Column("msgid", sa.String(length=256), nullable=False),
        sa.Column("raw_frame_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_deferred_passive_replies_case_expiry",
        "deferred_passive_replies",
        ["case_ref", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_deferred_passive_replies_expiry",
        "deferred_passive_replies",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_deferred_passive_replies_claim_token",
        "deferred_passive_replies",
        ["claim_token"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_deferred_passive_replies_claim_token", table_name="deferred_passive_replies"
    )
    op.drop_index("ix_deferred_passive_replies_expiry", table_name="deferred_passive_replies")
    op.drop_index(
        "ix_deferred_passive_replies_case_expiry", table_name="deferred_passive_replies"
    )
    op.drop_table("deferred_passive_replies")
