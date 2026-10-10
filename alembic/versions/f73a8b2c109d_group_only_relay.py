"""Move relay ownership to group display names and retire personal flows.

Revision ID: f73a8b2c109d
Revises: 9a4e7c2d1f60
Create Date: 2026-10-10 00:00:00.000000
"""

from collections.abc import Mapping, Sequence
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "f73a8b2c109d"
down_revision: str | Sequence[str] | None = "9a4e7c2d1f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cases", sa.Column("consult_handler_name", sa.String(length=256), nullable=True)
    )

    cases = sa.table(
        "cases",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("current_consultant_id", sa.Uuid(as_uuid=True)),
        sa.column("consult_handler_name", sa.String(length=256)),
    )
    users = sa.table(
        "users",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("display_name", sa.String(length=256)),
    )
    bind = op.get_bind()
    for row in bind.execute(
        sa.select(cases.c.id, cases.c.current_consultant_id)
    ).all():
        name = None
        if row.current_consultant_id is not None:
            name = bind.scalar(
                sa.select(users.c.display_name).where(users.c.id == row.current_consultant_id)
            )
        bind.execute(
            sa.update(cases)
            .where(cases.c.id == row.id)
            .values(consult_handler_name=name or "咨询团队")
        )
    with op.batch_alter_table("cases") as batch_op:
        batch_op.alter_column(
            "consult_handler_name",
            existing_type=sa.String(length=256),
            nullable=False,
        )

    deliveries = sa.table(
        "deliveries",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("entry_id", sa.Uuid(as_uuid=True)),
        sa.column("destination_type", sa.String(length=32)),
        sa.column("destination_address", sa.String(length=256)),
        sa.column("status", sa.String(length=32)),
        sa.column("attempts", sa.Integer()),
        sa.column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.column("last_error", sa.Text()),
        sa.column("lock_token", sa.Uuid(as_uuid=True)),
        sa.column("locked_until", sa.DateTime(timezone=True)),
    )
    case_entries = sa.table(
        "case_entries",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("case_id", sa.Uuid(as_uuid=True)),
    )
    routing_cases = sa.table(
        "cases",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("consult_queue_id", sa.Uuid(as_uuid=True)),
    )
    teams = sa.table(
        "teams",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("kind", sa.String(length=32)),
        sa.column("active", sa.Boolean()),
    )
    channels = sa.table(
        "wecom_channels",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("team_id", sa.Uuid(as_uuid=True)),
        sa.column("chatid", sa.String(length=256)),
        sa.column("active", sa.Boolean()),
        sa.column("initialized_at", sa.DateTime(timezone=True)),
    )
    items = sa.table(
        "delivery_items",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("delivery_id", sa.Uuid(as_uuid=True)),
        sa.column("req_id", sa.String(length=128)),
        sa.column("status", sa.String(length=32)),
        sa.column("platform_result_json", sa.JSON()),
        sa.column("last_error", sa.Text()),
    )

    pending_users = bind.execute(
        sa.select(deliveries.c.id, routing_cases.c.consult_queue_id)
        .select_from(
            deliveries.join(case_entries, case_entries.c.id == deliveries.c.entry_id).join(
                routing_cases, routing_cases.c.id == case_entries.c.case_id
            )
        )
        .where(
            deliveries.c.destination_type == "user",
            deliveries.c.status == "pending",
        )
    ).all()
    for delivery_id, queue_id in pending_users:
        chatids = bind.execute(
            sa.select(channels.c.chatid)
            .select_from(
                channels.join(teams, teams.c.id == channels.c.team_id)
            )
            .where(
                channels.c.team_id == queue_id,
                channels.c.active.is_(True),
                channels.c.initialized_at.is_not(None),
                teams.c.kind == "consult_queue",
                teams.c.active.is_(True),
            )
        ).scalars().all()
        if len(chatids) == 1:
            bind.execute(
                sa.update(deliveries)
                .where(deliveries.c.id == delivery_id)
                .values(destination_type="chat", destination_address=chatids[0])
            )
            bind.execute(
                sa.update(deliveries)
                .where(deliveries.c.id == delivery_id)
                .values(
                    attempts=0,
                    next_attempt_at=sa.func.current_timestamp(),
                    last_error=None,
                    lock_token=None,
                    locked_until=None,
                )
            )
            for item_id in bind.execute(
                sa.select(items.c.id).where(items.c.delivery_id == delivery_id)
            ).scalars():
                bind.execute(
                    sa.update(items)
                    .where(items.c.id == item_id)
                    .values(
                        req_id=f"kefu-{uuid4()}",
                        status="pending",
                        platform_result_json=None,
                        last_error=None,
                    )
                )
            continue

        error = "个人投递已停用，且咨询队列没有唯一的已绑定群聊"
        bind.execute(
            sa.update(deliveries)
            .where(deliveries.c.id == delivery_id)
            .values(
                status="failed",
                last_error=error,
                lock_token=None,
                locked_until=None,
            )
        )
        bind.execute(
            sa.update(items)
            .where(items.c.delivery_id == delivery_id, items.c.status == "pending")
            .values(status="failed", last_error=error)
        )

    deferred_replies = sa.table(
        "deferred_passive_replies",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("raw_frame_json", sa.JSON()),
    )
    for reply_id, raw_frame in bind.execute(
        sa.select(deferred_replies.c.id, deferred_replies.c.raw_frame_json)
    ).all():
        if not isinstance(raw_frame, Mapping):
            continue
        clean_frame = dict(raw_frame)
        body = clean_frame.get("body")
        if isinstance(body, Mapping):
            clean_body = dict(body)
            sender = clean_body.get("from")
            if isinstance(sender, Mapping):
                clean_sender = dict(sender)
                clean_sender.pop("userid", None)
                clean_body["from"] = clean_sender
            clean_frame["body"] = clean_body
        bind.execute(
            sa.update(deferred_replies)
            .where(deferred_replies.c.id == reply_id)
            .values(raw_frame_json=clean_frame)
        )

    op.drop_table("message_draft_parts")
    op.drop_table("message_drafts")
    op.drop_index("ix_users_wecom_open_userid", table_name="users")
    op.drop_column("users", "wecom_open_userid")


def downgrade() -> None:
    op.add_column("users", sa.Column("wecom_open_userid", sa.String(length=128), nullable=True))
    op.create_index(
        "ix_users_wecom_open_userid", "users", ["wecom_open_userid"], unique=True
    )
    op.create_table(
        "message_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("source_msgid", sa.String(length=256), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_msgid", name="uq_message_drafts_source_msgid"),
    )
    op.create_index("ix_message_drafts_expires_at", "message_drafts", ["expires_at"])
    op.create_index("ix_message_drafts_owner_user_id", "message_drafts", ["owner_user_id"])
    op.create_table(
        "message_draft_parts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("text", "image", name="partkind", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("media_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(kind = 'text' AND text IS NOT NULL AND media_id IS NULL) "
            "OR (kind = 'image' AND media_id IS NOT NULL AND text IS NULL)",
            name="ck_message_draft_parts_payload_matches_kind",
        ),
        sa.CheckConstraint("position >= 0", name="ck_message_draft_parts_nonnegative_position"),
        sa.ForeignKeyConstraint(["draft_id"], ["message_drafts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_id"], ["stored_media.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "draft_id", "position", name="uq_message_draft_parts_draft_position"
        ),
    )
    op.drop_column("cases", "consult_handler_name")
