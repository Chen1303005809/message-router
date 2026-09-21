"""Add event customer/priority metadata and purge the pre-tracker event history.

Revision ID: d4e6a81c9b20
Revises: c9e4b7a1d2f3
Create Date: 2026-09-20 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op
from kefu.config import Settings
from kefu.media.storage import object_storage_from_settings

revision: str = "d4e6a81c9b20"
down_revision: str | Sequence[str] | None = "c9e4b7a1d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Clear prior event data and add metadata used by the new H5 workbench."""
    if context.is_offline_mode():
        raise RuntimeError(
            "This migration deletes legacy media from object storage and must run online."
        )

    # All ingested images and draft attachments use the media/ prefix. Remove
    # the objects before their database records so a failure stops the upgrade
    # instead of silently retaining files with no metadata.
    object_storage_from_settings(Settings.from_env()).delete_prefix("media/")

    # Preserve organization and authorization configuration, while removing
    # every event, message, delivery, draft, callback cache, and media record.
    bind = op.get_bind()
    for statement in (
        "DELETE FROM case_entry_parts",
        "DELETE FROM message_draft_parts",
        "DELETE FROM delivery_items",
        "DELETE FROM deliveries",
        "UPDATE case_entries SET corrects_entry_id = NULL",
        "DELETE FROM case_entries",
        "UPDATE cases SET related_case_id = NULL",
        "DELETE FROM cases",
        "DELETE FROM message_drafts",
        "DELETE FROM inbound_messages",
        "DELETE FROM deferred_passive_replies",
        "DELETE FROM stored_media",
    ):
        bind.execute(sa.text(statement))

    op.add_column(
        "cases",
        sa.Column(
            "customer_name",
            sa.String(length=256),
            nullable=False,
            server_default="未录入",
        ),
    )
    op.add_column(
        "cases", sa.Column("customer_contact_name", sa.String(length=256), nullable=True)
    )
    op.add_column(
        "cases", sa.Column("customer_contact_method", sa.String(length=256), nullable=True)
    )
    op.add_column(
        "cases",
        sa.Column(
            "priority",
            sa.Enum(
                "normal",
                "urgent",
                "severe",
                name="casepriority",
                native_enum=False,
                length=16,
            ),
            nullable=False,
            server_default="normal",
        ),
    )
    op.create_index("ix_cases_priority", "cases", ["priority"], unique=False)


def downgrade() -> None:
    """Drop metadata columns; the intentionally purged event data cannot return."""
    op.drop_index("ix_cases_priority", table_name="cases")
    op.drop_column("cases", "priority")
    op.drop_column("cases", "customer_contact_method")
    op.drop_column("cases", "customer_contact_name")
    op.drop_column("cases", "customer_name")
