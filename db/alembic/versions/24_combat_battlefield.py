"""Combat grid: the battlefield of an encounter (size and scenery); squares stay on the combatant rows.

Revision ID: 24_combat_battlefield
Revises: 23_campaign_creation_requests
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "24_combat_battlefield"
down_revision: str | Sequence[str] | None = "23_campaign_creation_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "combat_state",
        sa.Column("battlefield", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("combat_state", "battlefield")
