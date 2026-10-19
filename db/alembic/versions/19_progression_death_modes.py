"""Explicit progression and death modes (EDG-045) and per-campaign character level.

Revision ID: 19_progression_death_modes
Revises: 18_campaign_membership_invites
Create Date: 2026-10-18

Campaign settings (both defaulted to the behaviour every existing campaign already has):
  * progression_mode: 'fixed' (the sheet keeps its level) or 'milestone' (engine-granted milestones,
    spent on explicit level-ups).
  * death_mode: 'subdued' (a defeat leaves the hero unconscious at 1 HP, the solo continuation) or
    'death_saves' (RAW death saving throws at 0 HP).

Per-campaign character state (assignment rows, which own the sheet's mutable values):
  * level: the hero's level in this campaign. Backfilled from the library character, so a sheet
    that already exists keeps its level. Level-ups change only this column.
  * milestones_pending: milestones the engine granted that the player has not yet spent.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "19_progression_death_modes"
down_revision: str | Sequence[str] | None = "18_campaign_membership_invites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "progression_mode", sa.String(16), nullable=False, server_default=sa.text("'fixed'")
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column("death_mode", sa.String(16), nullable=False, server_default=sa.text("'subdued'")),
    )
    op.create_check_constraint(
        "campaigns_progression_mode_valid",
        "campaigns",
        "progression_mode IN ('fixed', 'milestone')",
    )
    op.create_check_constraint(
        "campaigns_death_mode_valid", "campaigns", "death_mode IN ('subdued', 'death_saves')"
    )

    op.add_column(
        "character_assignments",
        sa.Column("level", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    # Existing assignments keep the level their library character was created at.
    op.execute(
        "UPDATE character_assignments AS ca SET level = c.level "
        "FROM characters AS c WHERE c.id = ca.character_id"
    )
    op.create_check_constraint(
        "character_assignments_level_valid", "character_assignments", "level BETWEEN 1 AND 20"
    )
    op.add_column(
        "character_assignments",
        sa.Column("milestones_pending", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint(
        "character_assignments_milestones_nonnegative",
        "character_assignments",
        "milestones_pending >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "character_assignments_milestones_nonnegative", "character_assignments", type_="check"
    )
    op.drop_column("character_assignments", "milestones_pending")
    op.drop_constraint("character_assignments_level_valid", "character_assignments", type_="check")
    op.drop_column("character_assignments", "level")
    op.drop_constraint("campaigns_death_mode_valid", "campaigns", type_="check")
    op.drop_constraint("campaigns_progression_mode_valid", "campaigns", type_="check")
    op.drop_column("campaigns", "death_mode")
    op.drop_column("campaigns", "progression_mode")
