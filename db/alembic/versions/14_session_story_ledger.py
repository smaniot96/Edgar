"""Add session-owned accepted facts without rewriting existing campaign data."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "14_session_story_ledger"
down_revision = "13_campaign_rules_edition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "story_facts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("effect_id", sa.String(64), nullable=False),
        sa.Column("fact_id", sa.String(64), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["event_log.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "effect_id", name="uq_story_facts_session_effect"),
    )
    op.add_column("npcs", sa.Column("session_id", sa.Integer(), nullable=True))
    op.add_column("npcs", sa.Column("story_identity", sa.String(64), nullable=True))
    op.create_foreign_key(
        "npcs_session_id_fkey", "npcs", "sessions", ["session_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_npcs_session_id", "npcs", ["session_id"])
    op.create_unique_constraint(
        "uq_npcs_session_story_identity", "npcs", ["session_id", "story_identity"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_npcs_session_story_identity", "npcs", type_="unique")
    op.drop_index("ix_npcs_session_id", table_name="npcs")
    op.drop_constraint("npcs_session_id_fkey", "npcs", type_="foreignkey")
    op.drop_column("npcs", "story_identity")
    op.drop_column("npcs", "session_id")
    op.drop_table("story_facts")
