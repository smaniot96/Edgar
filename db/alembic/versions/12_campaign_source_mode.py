"""Explicit homebrew choice; existing campaigns retain module bindings and behavior guards."""

import sqlalchemy as sa
from alembic import op

revision = "12_campaign_source_mode"
down_revision = "11_character_bounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("source_mode", sa.String(16), nullable=False, server_default=sa.text("'module'")),
    )
    op.create_check_constraint(
        "campaigns_source_mode_valid", "campaigns", "source_mode IN ('module', 'homebrew')"
    )


def downgrade() -> None:
    op.drop_constraint("campaigns_source_mode_valid", "campaigns", type_="check")
    op.drop_column("campaigns", "source_mode")
