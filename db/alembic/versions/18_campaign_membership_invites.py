"""Campaign memberships, invites and session participant roster (EDG-046).

Revision ID: 18_campaign_membership_invites
Revises: 17_api_tokens
Create Date: 2026-10-18

Purely additive: no existing row is rewritten. A campaign's creator stays the implicit owner
(no membership row), so existing single-owner campaigns and sessions are unchanged. Invite token
digests are unique; the plaintext is never stored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "18_campaign_membership_invites"
down_revision: str | Sequence[str] | None = "17_api_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_memberships",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'player', 'spectator')", name="campaign_memberships_role_valid"
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "user_id", name="uq_campaign_membership_user"),
    )
    op.create_index(
        op.f("ix_campaign_memberships_campaign_id"),
        "campaign_memberships",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_campaign_memberships_user_id"), "campaign_memberships", ["user_id"], unique=False
    )

    op.create_table(
        "campaign_invites",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), server_default=sa.text("'player'"), nullable=False),
        sa.Column("invited_email", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('player', 'spectator')", name="campaign_invites_role_valid"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["accepted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        op.f("ix_campaign_invites_campaign_id"), "campaign_invites", ["campaign_id"], unique=False
    )
    op.create_index(
        op.f("ix_campaign_invites_session_id"), "campaign_invites", ["session_id"], unique=False
    )

    op.create_table(
        "session_participants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), server_default=sa.text("'player'"), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('player')", name="session_participants_role_valid"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "user_id", name="uq_session_participant_user"),
        sa.UniqueConstraint("session_id", "character_id", name="uq_session_participant_character"),
    )
    op.create_index(
        op.f("ix_session_participants_session_id"),
        "session_participants",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_session_participants_user_id"), "session_participants", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_session_participants_character_id"),
        "session_participants",
        ["character_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_session_participants_character_id"), table_name="session_participants")
    op.drop_index(op.f("ix_session_participants_user_id"), table_name="session_participants")
    op.drop_index(op.f("ix_session_participants_session_id"), table_name="session_participants")
    op.drop_table("session_participants")
    op.drop_index(op.f("ix_campaign_invites_session_id"), table_name="campaign_invites")
    op.drop_index(op.f("ix_campaign_invites_campaign_id"), table_name="campaign_invites")
    op.drop_table("campaign_invites")
    op.drop_index(op.f("ix_campaign_memberships_user_id"), table_name="campaign_memberships")
    op.drop_index(op.f("ix_campaign_memberships_campaign_id"), table_name="campaign_memberships")
    op.drop_table("campaign_memberships")
