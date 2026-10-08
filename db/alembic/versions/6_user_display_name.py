"""Add display_name column to users table.

Revision ID: 6_user_display_name
Revises: 5_combat_state_extensions
Create Date: 2026-05-10

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6_user_display_name"
down_revision: str | Sequence[str] | None = "5_combat_state_extensions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("display_name", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "display_name")
