"""Pin campaign source selection; legacy source editions remain unspecified."""

import sqlalchemy as sa
from alembic import op

revision = "13_campaign_rules_edition"
down_revision = "12_campaign_source_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("rules_edition", sa.String(16), nullable=False, server_default=sa.text("'2014'")),
    )
    op.create_check_constraint(
        "campaigns_rules_edition_valid", "campaigns", "rules_edition IN ('2014', '2024')"
    )


def downgrade() -> None:
    op.drop_constraint("campaigns_rules_edition_valid", "campaigns", type_="check")
    op.drop_column("campaigns", "rules_edition")
