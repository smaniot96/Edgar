"""Per-campaign "always confirm" for combat declarations (EDG-182).

Revision ID: 21_combat_always_confirm
Revises: 20_spotlight_turn_arbitration
Create Date: 2026-10-19

A campaign setting, default false (the confirmation that multi-part and conditional turns always get).
When true, every turn that declares an action is repeated back before any die, for every hero in the
campaign. Reversible: the downgrade drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "21_combat_always_confirm"
down_revision: str | Sequence[str] | None = "20_spotlight_turn_arbitration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "combat_always_confirm", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "combat_always_confirm")
