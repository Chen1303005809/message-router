"""Add private H5 handoffs and group intake turn authors."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8b31d9f2c0a7"
down_revision: str | Sequence[str] | None = "6d4e8f2a1c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "private_intake_handoffs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("msgid", sa.String(length=256), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("parts_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token", name="uq_private_intake_handoffs_token"),
        sa.UniqueConstraint("msgid", name="uq_private_intake_handoffs_msgid"),
    )
    op.create_index(
        "ix_private_intake_handoffs_actor_id",
        "private_intake_handoffs",
        ["actor_id"],
    )
    op.create_index(
        "ix_private_intake_handoffs_expiry",
        "private_intake_handoffs",
        ["expires_at"],
    )
    with op.batch_alter_table("issue_intake_drafts") as batch_op:
        batch_op.add_column(
            sa.Column("private_handoff_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_issue_intake_drafts_private_handoff_id_handoffs",
            "private_intake_handoffs",
            ["private_handoff_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_issue_intake_drafts_private_handoff_id",
            ["private_handoff_id"],
            unique=True,
        )
    with op.batch_alter_table("group_issue_intake_turns") as batch_op:
        batch_op.add_column(
            sa.Column("actor_user_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_group_issue_intake_turns_actor_user_id_users",
            "users",
            ["actor_user_id"],
            ["id"],
        )
        batch_op.add_column(
            sa.Column("actor_name_snapshot", sa.String(length=256), nullable=True)
        )
        batch_op.create_index(
            "ix_group_issue_intake_turns_actor_user_id",
            ["actor_user_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("group_issue_intake_turns") as batch_op:
        batch_op.drop_index("ix_group_issue_intake_turns_actor_user_id")
        batch_op.drop_constraint(
            "fk_group_issue_intake_turns_actor_user_id_users",
            type_="foreignkey",
        )
        batch_op.drop_column("actor_name_snapshot")
        batch_op.drop_column("actor_user_id")
    with op.batch_alter_table("issue_intake_drafts") as batch_op:
        batch_op.drop_index("ix_issue_intake_drafts_private_handoff_id")
        batch_op.drop_constraint(
            "fk_issue_intake_drafts_private_handoff_id_handoffs",
            type_="foreignkey",
        )
        batch_op.drop_column("private_handoff_id")
    op.drop_index("ix_private_intake_handoffs_expiry", table_name="private_intake_handoffs")
    op.drop_index("ix_private_intake_handoffs_actor_id", table_name="private_intake_handoffs")
    op.drop_table("private_intake_handoffs")
