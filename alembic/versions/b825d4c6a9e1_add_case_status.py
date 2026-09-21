"""Add a workflow status to cases.

Revision ID: b825d4c6a9e1
Revises: 4b7d9f2a6c31
Create Date: 2026-09-21 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b825d4c6a9e1"
down_revision: str | Sequence[str] | None = "4b7d9f2a6c31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add workflow states, preserving the closed state of existing cases."""
    op.add_column(
        "cases",
        sa.Column(
            "status",
            sa.Enum(
                "pending_confirmation",
                "in_progress",
                "waiting_customer",
                "closed",
                "suspended",
                name="casestatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
            server_default="pending_confirmation",
        ),
    )
    cases = sa.table(
        "cases",
        sa.column("lifecycle_status", sa.String(length=16)),
        sa.column("status", sa.String(length=32)),
    )
    op.execute(
        cases.update()
        .where(cases.c.lifecycle_status == "closed")
        .values(status="closed")
    )


def downgrade() -> None:
    """Remove the workflow status column."""
    op.drop_column("cases", "status")
