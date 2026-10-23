"""Track active workbench Agent draft requests in the task queue."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a172c9e46b30"
down_revision: str | Sequence[str] | None = "e2b7f3a91c6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issue_draft_processing_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_issue_draft_processing_tasks_started_at",
        "issue_draft_processing_tasks",
        ["started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_issue_draft_processing_tasks_started_at",
        table_name="issue_draft_processing_tasks",
    )
    op.drop_table("issue_draft_processing_tasks")
