"""Durable new-playthrough request identities; existing campaigns remain unchanged.

Revision ID: 23_campaign_creation_requests
Revises: 22_table_pacing_queued_actions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "23_campaign_creation_requests"
down_revision: str | Sequence[str] | None = "22_table_pacing_queued_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("creation_request_id", sa.String(36), nullable=True))
    op.add_column("campaigns", sa.Column("creation_request_hash", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_campaign_creation_request", "campaigns", ["created_by", "creation_request_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_campaign_creation_request", "campaigns", type_="unique")
    op.drop_column("campaigns", "creation_request_hash")
    op.drop_column("campaigns", "creation_request_id")
