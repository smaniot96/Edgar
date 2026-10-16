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
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM combat_state WHERE active_character IS NULL")
    )
    if count:
        raise RuntimeError(
            f"Combat active_character downgrade refused: combat_state: {count} row(s) hold no "
            "active_character, which revision 4 requires. Restore from a pre-migration backup "
            "instead; no data was changed."
        )
    op.alter_column(
        "combat_state",
        "active_character",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_column("combat_state", "ended")
    op.drop_column("combat_state", "current_turn_index")
