"""campaign adventure_collections and session current_scene_id

Revision ID: 3_campaign_adv
Revises: 2_add_event_log
Create Date: 2026-03-16

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3_campaign_adv"
down_revision: str | Sequence[str] | None = "2_add_event_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "adventure_collections",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "sessions",
        sa.Column("current_scene_id", sa.String(length=255), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE campaigns SET adventure_collections = ARRAY['chalice_of_the_mountain_god']::varchar[] "
            "WHERE cardinality(adventure_collections) = 0"
        )
    )


def downgrade() -> None:
    op.drop_column("sessions", "current_scene_id")
    op.drop_column("campaigns", "adventure_collections")
