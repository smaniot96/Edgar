"""session active_character_id FK

Revision ID: 4_session_active_character
Revises: 3_campaign_adv
Create Date: 2026-05-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4_session_active_character"
down_revision: Union[str, Sequence[str], None] = "3_campaign_adv"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("active_character_id", sa.Integer(), sa.ForeignKey("characters.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "active_character_id")
