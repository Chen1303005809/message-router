"""Persist inspection schedule and team filters.

Revision ID: 2f6a9c1d4e80
Revises: b7d9a3e1c640
Create Date: 2026-10-29 00:00:00.000000
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "2f6a9c1d4e80"
down_revision: str | Sequence[str] | None = "b7d9a3e1c640"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch_op:
        batch_op.add_column(sa.Column("inspection_age_hours", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "inspection_own_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.create_check_constraint(
            "ck_teams_inspection_age_hours_positive",
            "inspection_age_hours IS NULL OR inspection_age_hours > 0",
        )

    op.create_table(
        "inspection_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("times", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    schedule_table = sa.table(
        "inspection_schedules",
        sa.column("id", sa.Integer()),
        sa.column("times", sa.JSON()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        schedule_table,
        [
            {
                "id": 1,
                "times": ["08:55", "11:30", "21:30"],
                "updated_at": datetime.now(UTC),
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("inspection_schedules")
    with op.batch_alter_table("teams") as batch_op:
        batch_op.drop_constraint("ck_teams_inspection_age_hours_positive", type_="check")
        batch_op.drop_column("inspection_own_only")
        batch_op.drop_column("inspection_age_hours")
