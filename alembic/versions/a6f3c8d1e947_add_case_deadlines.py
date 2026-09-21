"""Add case deadlines and configurable approaching windows.

Revision ID: a6f3c8d1e947
Revises: d4e6a81c9b20
Create Date: 2026-09-20 18:00:00.000000
"""

from collections.abc import Sequence
from datetime import UTC, timedelta

import sqlalchemy as sa

from alembic import context, op

revision: str = "a6f3c8d1e947"
down_revision: str | Sequence[str] | None = "d4e6a81c9b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add deadline fields and initialize existing events from their creation time."""
    if context.is_offline_mode():
        raise RuntimeError(
            "The case deadline migration must run online to backfill existing events."
        )

    op.add_column(
        "cases",
        sa.Column(
            "deadline",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.add_column(
        "cases",
        sa.Column(
            "approaching_window_minutes",
            sa.Integer(),
            nullable=False,
            server_default="1440",
        ),
    )

    cases = sa.table(
        "cases",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("deadline", sa.DateTime(timezone=True)),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.select(cases.c.id, cases.c.created_at)).all()
    for row in rows:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        bind.execute(
            sa.update(cases)
            .where(cases.c.id == row.id)
            .values(deadline=created_at + timedelta(hours=3))
        )

    # SQLite cannot ALTER COLUMN to drop a default; the Python service always
    # supplies a creation-relative deadline, so the migration default is only
    # a harmless fallback there.
    if bind.dialect.name != "sqlite":
        op.alter_column("cases", "deadline", server_default=None)
        op.alter_column("cases", "approaching_window_minutes", server_default=None)


def downgrade() -> None:
    """Remove deadline fields."""
    op.drop_column("cases", "approaching_window_minutes")
    op.drop_column("cases", "deadline")
