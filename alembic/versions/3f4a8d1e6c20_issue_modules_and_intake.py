"""Reset event data and introduce issue-oriented storage and messaging."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3f4a8d1e6c20"
down_revision: str | Sequence[str] | None = "f73a8b2c109d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The approved cutover keeps users, teams, memberships, channels and
    # sessions, while resetting records tied to the old issue workflow.
    bind = op.get_bind()
    old_media_count = bind.execute(sa.text("SELECT COUNT(*) FROM stored_media")).scalar_one()
    for table in (
        "delivery_items",
        "deliveries",
        "deferred_passive_replies",
        "inbound_messages",
        "case_entry_parts",
        "case_entries",
        "cases",
        "stored_media",
    ):
        op.execute(sa.text(f"DELETE FROM {table}"))
    if old_media_count:
        from kefu.config import Settings
        from kefu.media.storage import object_storage_from_settings

        object_storage_from_settings(Settings.from_env()).delete_prefix("media/")

    op.rename_table("cases", "issues")
    op.rename_table("case_entries", "issue_entries")
    op.rename_table("case_entry_parts", "issue_entry_parts")

    for old_name in (
        "ix_cases_consult_queue_id",
        "ix_cases_current_consultant_id",
        "ix_cases_current_dev_team_id",
        "ix_cases_current_developer_id",
        "ix_cases_priority",
    ):
        op.drop_index(old_name, table_name="issues")
    op.create_index("ix_issues_consult_queue_id", "issues", ["consult_queue_id"])
    op.create_index("ix_issues_current_consultant_id", "issues", ["current_consultant_id"])
    op.create_index("ix_issues_current_dev_team_id", "issues", ["current_dev_team_id"])
    op.create_index("ix_issues_current_developer_id", "issues", ["current_developer_id"])

    with op.batch_alter_table("issues") as batch:
        batch.add_column(sa.Column("initial_acceptance_team_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_issues_initial_acceptance_team_id_teams",
            "teams",
            ["initial_acceptance_team_id"],
            ["id"],
        )
    op.create_index(
        "ix_issues_initial_acceptance_team_id", "issues", ["initial_acceptance_team_id"]
    )
    op.add_column("issues", sa.Column("issue_description", sa.Text(), nullable=True))
    op.add_column("issues", sa.Column("suspected_type", sa.String(128), nullable=True))
    op.add_column("issues", sa.Column("issue_current_situation", sa.Text(), nullable=True))
    op.add_column(
        "issues",
        sa.Column(
            "issue_current_status",
            sa.String(128),
            server_default="问题未回复",
            nullable=False,
        ),
    )
    op.add_column("issues", sa.Column("bug_number", sa.String(64), nullable=True))
    with op.batch_alter_table("issues") as batch:
        batch.create_unique_constraint("uq_issues_bug_number", ["bug_number"])
    op.add_column("issues", sa.Column("feedback_event_point", sa.Text(), nullable=True))
    op.add_column("issues", sa.Column("issue_source", sa.String(256), nullable=True))
    op.add_column(
        "issues",
        sa.Column("issue_metadata_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column("issues", sa.Column("initial_expires_at", sa.DateTime(timezone=True)))

    op.create_table(
        "issue_intake_drafts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("issue_number", sa.String(32), nullable=False, unique=True),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("fields_json", sa.JSON(), nullable=False),
        sa.Column("parts_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_issue_id", sa.Uuid(), sa.ForeignKey("issues.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_issue_intake_drafts_actor_id", "issue_intake_drafts", ["actor_id"])
    op.create_index("ix_issue_intake_drafts_expiry", "issue_intake_drafts", ["expires_at"])
    op.create_table(
        "issue_inspection_states",
        sa.Column(
            "issue_id",
            sa.Uuid(),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_periodic_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approaching_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "message_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column(
            "destination_type",
            sa.Enum("user", "chat", name="deliverydestination", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("destination_address", sa.String(256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "sent", "failed", name="deliverystatus", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("lock_token", sa.Uuid(), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_message_outbox_idempotency_key"),
    )
    op.create_index("ix_message_outbox_pending", "message_outbox", ["status", "next_attempt_at"])
    op.create_index("ix_message_outbox_lock_token", "message_outbox", ["lock_token"])


def downgrade() -> None:
    op.drop_index("ix_message_outbox_lock_token", table_name="message_outbox")
    op.drop_index("ix_message_outbox_pending", table_name="message_outbox")
    op.drop_table("message_outbox")
    op.drop_table("issue_inspection_states")
    op.drop_index("ix_issue_intake_drafts_expiry", table_name="issue_intake_drafts")
    op.drop_index("ix_issue_intake_drafts_actor_id", table_name="issue_intake_drafts")
    op.drop_table("issue_intake_drafts")
    op.drop_index("ix_issues_initial_acceptance_team_id", table_name="issues")
    for index_name in (
        "ix_issues_consult_queue_id",
        "ix_issues_current_consultant_id",
        "ix_issues_current_dev_team_id",
        "ix_issues_current_developer_id",
    ):
        op.drop_index(index_name, table_name="issues")
    with op.batch_alter_table("issues") as batch:
        batch.drop_constraint("uq_issues_bug_number", type_="unique")
        batch.drop_constraint(
            "fk_issues_initial_acceptance_team_id_teams",
            type_="foreignkey",
        )
        for column in (
            "initial_expires_at",
            "issue_metadata_json",
            "issue_source",
            "feedback_event_point",
            "bug_number",
            "issue_current_status",
            "issue_current_situation",
            "suspected_type",
            "issue_description",
            "initial_acceptance_team_id",
        ):
            batch.drop_column(column)
    op.rename_table("issue_entry_parts", "case_entry_parts")
    op.rename_table("issue_entries", "case_entries")
    op.rename_table("issues", "cases")
    op.create_index("ix_cases_consult_queue_id", "cases", ["consult_queue_id"])
    op.create_index("ix_cases_current_consultant_id", "cases", ["current_consultant_id"])
    op.create_index("ix_cases_current_dev_team_id", "cases", ["current_dev_team_id"])
    op.create_index("ix_cases_current_developer_id", "cases", ["current_developer_id"])
    op.create_index("ix_cases_priority", "cases", ["priority"])
