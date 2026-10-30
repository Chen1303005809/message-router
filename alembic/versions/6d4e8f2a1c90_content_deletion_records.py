"""Record issue and team deletion operations."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "6d4e8f2a1c90"
down_revision: str | Sequence[str] | None = "2f6a9c1d4e80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "content_deletion_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("parent_record_id", sa.Uuid(), nullable=True),
        sa.Column(
            "target_kind",
            sa.Enum(
                "issue",
                "team",
                name="deletiontargetkind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("target_label", sa.String(length=768), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_name", sa.String(length=256), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "succeeded",
                "failed",
                name="deletionrecordoutcome",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_content_deletion_records_created_at",
        "content_deletion_records",
        ["created_at"],
    )
    op.create_index(
        "ix_content_deletion_records_operation",
        "content_deletion_records",
        ["operation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_content_deletion_records_operation",
        table_name="content_deletion_records",
    )
    op.drop_index(
        "ix_content_deletion_records_created_at",
        table_name="content_deletion_records",
    )
    op.drop_table("content_deletion_records")
