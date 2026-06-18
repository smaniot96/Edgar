"""combat_state.combatants + outcome (per-combatant HP for solo-playable combat)

Revision ID: 9_combat_combatants
Revises: 8_character_assignments
Create Date: 2026-06-18

Adds a JSONB `combatants` column holding the full combatant roster with per-entity HP
(player + enemies) and an `outcome` string ('victory' | 'defeat' | 'fled' | null) so combat
can be resolved a full round per HTTP turn, track enemy deaths, and end deterministically.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "9_combat_combatants"
down_revision: Union[str, Sequence[str], None] = "8_character_assignments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "combat_state",
        sa.Column(
            "combatants",
            JSONB,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "combat_state",
        sa.Column("outcome", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("combat_state", "outcome")
    op.drop_column("combat_state", "combatants")
