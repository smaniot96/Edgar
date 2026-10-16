"""Retire the legacy combat_state.active_character pointer.

Revision ID: 15_retire_combat_active_character
Revises: 14_session_story_ledger
Create Date: 2026-10-16

No code writes or reads combat_state.active_character: combat load and persist use the JSON
roster (`combatants`), so the pointer carries no state the engine needs. Upgrade only drops the
column when every encounter holds NULL there. Otherwise it refuses with a row count and changes
nothing, so an operator decides whether that history is worth keeping and clears it explicitly.
Dropping an all-NULL column loses no data, so downgrade restores the nullable column, its
SET NULL foreign key and index. Restored values are NULL, which is exact for this round trip.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "15_retire_combat_active_character"
down_revision: str | Sequence[str] | None = "14_session_story_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM combat_state WHERE active_character IS NOT NULL")
    )
    if count:
        raise RuntimeError(
            f"Combat pointer retirement refused: combat_state: {count} row(s) hold a legacy "
            "active_character value. Copy any history that must survive, clear these values "
            "explicitly (SET active_character = NULL), then retry; no data was changed."
        )
    op.drop_column("combat_state", "active_character")


def downgrade() -> None:
    op.add_column("combat_state", sa.Column("active_character", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "combat_state_active_character_fkey",
        "combat_state",
        "characters",
        ["active_character"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_combat_state_active_character", "combat_state", ["active_character"])
