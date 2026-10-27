"""Add a dedicated customer group chat name to issues."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "b7d9a3e1c640"
down_revision: str | Sequence[str] | None = "a172c9e46b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "The customer group name migration must run online to backfill existing issues."
        )

    op.add_column(
        "issues",
        sa.Column("customer_group_name", sa.Text(), nullable=True),
    )

    issues = sa.table(
        "issues",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("issue_metadata_json", sa.JSON()),
        sa.column("customer_group_name", sa.Text()),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.select(issues.c.id, issues.c.issue_metadata_json)).all()
    for row in rows:
        metadata = row.issue_metadata_json
        if not isinstance(metadata, dict):
            continue
        group_name = metadata.get("customer_group_name") or metadata.get("source_group_name")
        if not isinstance(group_name, str) or not group_name.strip():
            continue
        bind.execute(
            sa.update(issues)
            .where(issues.c.id == row.id)
            .values(customer_group_name=group_name.strip())
        )


def downgrade() -> None:
    op.drop_column("issues", "customer_group_name")
