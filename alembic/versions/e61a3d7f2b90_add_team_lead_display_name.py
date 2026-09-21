"""Add a display-only lead label to development teams.

Revision ID: e61a3d7f2b90
Revises: b825d4c6a9e1
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e61a3d7f2b90"
down_revision: str | Sequence[str] | None = "b825d4c6a9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("teams", sa.Column("lead_display_name", sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column("teams", "lead_display_name")
