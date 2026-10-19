"""Server-side spotlight turn arbitration for multi-PC tables (EDG-048).

Revision ID: 20_spotlight_turn_arbitration
Revises: 19_progression_death_modes
Create Date: 2026-10-19

Session columns (additive; every existing session gets the defaults, which mean the same thing as
before for solo and single-PC tables, where the rules are never engaged):
  * spotlight_mode: 'round_robin' (default for multi-PC tables), 'owner_directed' or 'free_for_all'.
  * spotlight_version: monotonic counter bumped on every holder, rotation or mode change.
  * spotlight_timeout_seconds: how long an idle holder keeps the spotlight (30 to 3600 seconds).
  * spotlight_since: when the current holder last acted or received the spotlight.
  * spotlight_order: the round-robin rotation, as character ids in turn order.
  * spotlight_cooldowns: free-for-all cooldowns, per character, until the stored time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20_spotlight_turn_arbitration"
down_revision: str | Sequence[str] | None = "19_progression_death_modes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("spotlight_mode", sa.String(16), nullable=False, server_default=sa.text("'round_robin'")),
    )
    op.add_column(
        "sessions",
        sa.Column("spotlight_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "sessions",
        sa.Column("spotlight_timeout_seconds", sa.Integer(), nullable=False, server_default=sa.text("300")),
    )
    op.add_column(
        "sessions",
        sa.Column("spotlight_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "spotlight_order",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "spotlight_cooldowns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "sessions_spotlight_mode_valid",
        "sessions",
        "spotlight_mode IN ('round_robin', 'owner_directed', 'free_for_all')",
    )
    op.create_check_constraint(
        "sessions_spotlight_version_nonnegative", "sessions", "spotlight_version >= 0"
    )
    op.create_check_constraint(
        "sessions_spotlight_timeout_valid",
        "sessions",
        "spotlight_timeout_seconds BETWEEN 30 AND 3600",
    )


def downgrade() -> None:
    op.drop_constraint("sessions_spotlight_timeout_valid", "sessions", type_="check")
    op.drop_constraint("sessions_spotlight_version_nonnegative", "sessions", type_="check")
    op.drop_constraint("sessions_spotlight_mode_valid", "sessions", type_="check")
    op.drop_column("sessions", "spotlight_cooldowns")
    op.drop_column("sessions", "spotlight_order")
    op.drop_column("sessions", "spotlight_since")
    op.drop_column("sessions", "spotlight_timeout_seconds")
    op.drop_column("sessions", "spotlight_version")
    op.drop_column("sessions", "spotlight_mode")
