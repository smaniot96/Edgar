"""Retain scoped request identities for retry-safe hero/save creation.

Revision ID: 16_setup_creation_requests
Revises: 15_retire_combat_pointer
Create Date: 2026-10-16

Existing rows retain NULL identities; no source, history or assignment is rewritten.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "16_setup_creation_requests"
down_revision: str | Sequence[str] | None = "15_retire_combat_pointer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table, scope, constraint in (
        ("characters", "owner_user_id", "uq_character_creation_request"),
        ("sessions", "campaign_id", "uq_session_creation_request"),
    ):
        op.add_column(table, sa.Column("creation_request_id", sa.String(36), nullable=True))
        op.add_column(table, sa.Column("creation_request_hash", sa.String(64), nullable=True))
        op.create_unique_constraint(constraint, table, [scope, "creation_request_id"])


def downgrade() -> None:
    for table, constraint in (
        ("sessions", "uq_session_creation_request"),
        ("characters", "uq_character_creation_request"),
    ):
        op.drop_constraint(constraint, table, type_="unique")
        op.drop_column(table, "creation_request_hash")
        op.drop_column(table, "creation_request_id")
