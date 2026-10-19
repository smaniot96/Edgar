"""Table pacing policy and queued intended actions (EDG-051).

Revision ID: 22_table_pacing_queued_actions
Revises: 21_combat_always_confirm
Create Date: 2026-10-19

Session columns (additive; existing sessions get the defaults, which mean exactly what they meant
before: the 'standard' policy is the 300 second spotlight timeout from EDG-048, and nobody has
queued anything):
  * pacing_policy: 'relaxed', 'standard' (default), 'fast' or 'custom' (an explicit timeout was set).
  * queued_actions: {"revision": n, "items": {"<user_id>": {...}}}, at most one item per player.

Numbered 22, after 21_combat_always_confirm (EDG-182), at integration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "22_table_pacing_queued_actions"
down_revision: str | Sequence[str] | None = "21_combat_always_confirm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("pacing_policy", sa.String(16), nullable=False, server_default=sa.text("'standard'")),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "queued_actions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "sessions_pacing_policy_valid",
        "sessions",
        "pacing_policy IN ('relaxed', 'standard', 'fast', 'custom')",
    )


def downgrade() -> None:
    op.drop_constraint("sessions_pacing_policy_valid", "sessions", type_="check")
    op.drop_column("sessions", "queued_actions")
    op.drop_column("sessions", "pacing_policy")
