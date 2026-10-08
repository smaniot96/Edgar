"""combat_state current_turn_index, ended, nullable active_character

Revision ID: 5_combat_state_extensions
Revises: 4_session_active_character
Create Date: 2026-05-10

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5_combat_state_extensions"
down_revision: str | Sequence[str] | None = "4_session_active_character"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "combat_state",
        sa.Column(
            "current_turn_index",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "combat_state",
        sa.Column(
            "ended",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.alter_column(
        "combat_state",
        "active_character",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "combat_state",
        "active_character",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_column("combat_state", "ended")
    op.drop_column("combat_state", "current_turn_index")
