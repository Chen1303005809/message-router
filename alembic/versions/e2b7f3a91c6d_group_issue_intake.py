"""Persist group-based Agent intake conversations."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e2b7f3a91c6d"
down_revision: str | Sequence[str] | None = "c5d8e2a1b704"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("issues") as batch:
        batch.alter_column(
            "creator_id",
            existing_type=sa.Uuid(),
            nullable=True,
        )

    op.create_table(
        "group_issue_intake_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("chatid", sa.String(length=256), nullable=False),
        sa.Column("consult_queue_id", sa.Uuid(), nullable=False),
        sa.Column("source_msgid", sa.String(length=256), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("parts_json", sa.JSON(), nullable=False),
        sa.Column("fields_json", sa.JSON(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "active",
                "dispatching",
                "completed",
                name="groupissueintakestate",
                native_enum=False,
                length=16,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("issue_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["consult_queue_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_msgid", name="uq_group_issue_intake_sessions_source_msgid"),
        sa.UniqueConstraint("token", name="uq_group_issue_intake_sessions_token"),
    )
    op.create_index(
        "ix_group_issue_intake_sessions_chatid",
        "group_issue_intake_sessions",
        ["chatid"],
    )
    op.create_index(
        "ix_group_issue_intake_sessions_expiry",
        "group_issue_intake_sessions",
        ["state", "expires_at"],
    )
    op.create_table(
        "group_issue_intake_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("msgid", sa.String(length=256), nullable=False),
        sa.Column("user_text", sa.Text(), nullable=False),
        sa.Column("reply_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["group_issue_intake_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("msgid", name="uq_group_issue_intake_turns_msgid"),
    )
    op.create_index(
        "ix_group_issue_intake_turns_session_id",
        "group_issue_intake_turns",
        ["session_id"],
    )
    op.create_index(
        "ix_group_issue_intake_turns_session_created",
        "group_issue_intake_turns",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_group_issue_intake_turns_session_created", table_name="group_issue_intake_turns"
    )
    op.drop_index(
        "ix_group_issue_intake_turns_session_id", table_name="group_issue_intake_turns"
    )
    op.drop_table("group_issue_intake_turns")
    op.drop_index(
        "ix_group_issue_intake_sessions_expiry", table_name="group_issue_intake_sessions"
    )
    op.drop_index(
        "ix_group_issue_intake_sessions_chatid", table_name="group_issue_intake_sessions"
    )
    op.drop_table("group_issue_intake_sessions")
    with op.batch_alter_table("issues") as batch:
        batch.alter_column(
            "creator_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )
