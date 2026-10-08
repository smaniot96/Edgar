"""add event_log created_at default

Revision ID: 2_add_event_log
Revises: 1ad2ad6560f5
Create Date: 2026-03-18

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2_add_event_log"
down_revision: str | Sequence[str] | None = "1ad2ad6560f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "event_log",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    op.alter_column(
        "event_log",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=None,
    )
