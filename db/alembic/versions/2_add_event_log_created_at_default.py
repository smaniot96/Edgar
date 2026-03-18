"""add event_log created_at default

Revision ID: 2_add_event_log
Revises: 1ad2ad6560f5
Create Date: 2026-03-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2_add_event_log"
down_revision: Union[str, Sequence[str], None] = "1ad2ad6560f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
