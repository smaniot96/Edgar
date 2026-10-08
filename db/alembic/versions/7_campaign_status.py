"""campaign status + ended_at (plan 02; needed for plan 03 assignment rules)

Revision ID: 7_campaign_status
Revises: 6_user_display_name
Create Date: 2026-05-10

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7_campaign_status"
down_revision: str | Sequence[str] | None = "6_user_display_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "status",
            sa.String(16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "campaigns_status_valid",
        "campaigns",
        "status IN ('active', 'ended')",
    )


def downgrade() -> None:
    op.drop_constraint("campaigns_status_valid", "campaigns", type_="check")
    op.drop_column("campaigns", "ended_at")
    op.drop_column("campaigns", "status")
