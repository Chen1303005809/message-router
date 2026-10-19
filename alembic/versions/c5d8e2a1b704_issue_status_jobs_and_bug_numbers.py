"""Add status evaluation jobs and date-scoped bug numbering."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5d8e2a1b704"
down_revision: str | Sequence[str] | None = "3f4a8d1e6c20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("issues") as batch:
        batch.drop_constraint("uq_issues_bug_number", type_="unique")
        batch.drop_column("feedback_event_point")

    op.create_table(
        "bug_daily_sequences",
        sa.Column("date_key", sa.String(length=10), primary_key=True),
        sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "issue_status_update_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Uuid(),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "entry_id",
            sa.Uuid(),
            sa.ForeignKey("issue_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entry_sequence", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "processing",
                "completed",
                "cancelled",
                name="issuestatusupdatestate",
                native_enum=False,
                length=16,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lock_token", sa.Uuid(), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("entry_id", name="uq_issue_status_update_jobs_entry_id"),
    )
    op.create_index(
        "ix_issue_status_update_jobs_pending",
        "issue_status_update_jobs",
        ["state", "next_attempt_at"],
    )
    op.create_index(
        "ix_issue_status_update_jobs_issue_sequence",
        "issue_status_update_jobs",
        ["case_id", "entry_sequence"],
    )
    op.create_index(
        "ix_issue_status_update_jobs_case_id", "issue_status_update_jobs", ["case_id"]
    )
    op.create_index(
        "ix_issue_status_update_jobs_lock_token", "issue_status_update_jobs", ["lock_token"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    duplicate_bug_number = bind.execute(
        sa.text(
            "SELECT bug_number FROM issues WHERE bug_number IS NOT NULL "
            "GROUP BY bug_number HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_bug_number is not None:
        raise RuntimeError("无法恢复 Bug 编号全局唯一约束：存在跨年重复编号")

    op.drop_index("ix_issue_status_update_jobs_lock_token", table_name="issue_status_update_jobs")
    op.drop_index("ix_issue_status_update_jobs_case_id", table_name="issue_status_update_jobs")
    op.drop_index(
        "ix_issue_status_update_jobs_issue_sequence", table_name="issue_status_update_jobs"
    )
    op.drop_index("ix_issue_status_update_jobs_pending", table_name="issue_status_update_jobs")
    op.drop_table("issue_status_update_jobs")
    op.drop_table("bug_daily_sequences")
    with op.batch_alter_table("issues") as batch:
        batch.add_column(sa.Column("feedback_event_point", sa.Text(), nullable=True))
        batch.create_unique_constraint("uq_issues_bug_number", ["bug_number"])
