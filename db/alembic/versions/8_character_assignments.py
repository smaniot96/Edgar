"""character library + character_assignments (plan 03)

Revision ID: 8_character_assignments
Revises: 7_campaign_status
Create Date: 2026-05-10

Downgrading past this revision loses assignment history (lossy).
"""
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8_character_assignments"
down_revision: Union[str, Sequence[str], None] = "7_campaign_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_characters_campaign_fk(bind: Any) -> None:
    insp = sa.inspect(bind)
    for fk in insp.get_foreign_keys("characters"):
        if fk.get("constrained_columns") == ["campaign_id"]:
            name = fk.get("name")
            if name:
                op.drop_constraint(name, "characters", type_="foreignkey")
            return


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.add_column("characters", sa.Column("base_stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column(
        "characters",
        sa.Column("base_inventory", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "characters",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.execute(
        """
        UPDATE characters c
        SET base_stats = c.stats,
            base_inventory = c.inventory,
            owner_user_id = camp.created_by
        FROM campaigns camp
        WHERE camp.id = c.campaign_id
        """
    )

    op.create_table(
        "character_assignments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("character_id", sa.Integer(), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("hp_current", sa.Integer(), nullable=False),
        sa.Column("hp_max", sa.Integer(), nullable=False),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("inventory", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        """
        INSERT INTO character_assignments
            (character_id, campaign_id, hp_current, hp_max, stats, inventory)
        SELECT id, campaign_id, hp_current, hp_max, stats, inventory
        FROM characters
        """
    )

    op.create_index(
        "character_assignments_one_active",
        "character_assignments",
        ["character_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    bind = op.get_bind()
    assert bind is not None
    _drop_characters_campaign_fk(bind)
    op.drop_column("characters", "campaign_id")
    op.drop_column("characters", "hp_current")
    op.drop_column("characters", "stats")
    op.drop_column("characters", "inventory")

    op.alter_column("characters", "owner_user_id", nullable=False)
    op.alter_column("characters", "base_stats", nullable=False)
    op.alter_column("characters", "base_inventory", nullable=False)


def downgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("campaign_id", sa.Integer(), nullable=True),
    )
    op.add_column("characters", sa.Column("hp_current", sa.Integer(), nullable=True))
    op.add_column("characters", sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("characters", sa.Column("inventory", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.execute(
        """
        UPDATE characters c SET
            campaign_id = ca.campaign_id,
            hp_current = ca.hp_current,
            stats = ca.stats,
            inventory = ca.inventory
        FROM character_assignments ca
        WHERE c.id = ca.character_id AND ca.ended_at IS NULL
        """
    )

    op.drop_index("character_assignments_one_active", table_name="character_assignments")
    op.drop_table("character_assignments")

    op.create_foreign_key(None, "characters", "campaigns", ["campaign_id"], ["id"])

    op.alter_column("characters", "campaign_id", nullable=False)
    op.alter_column("characters", "hp_current", nullable=False)
    op.alter_column("characters", "stats", nullable=False)
    op.alter_column("characters", "inventory", nullable=False)

    op.drop_column("characters", "created_at")
    op.drop_column("characters", "base_inventory")
    op.drop_column("characters", "base_stats")
    op.drop_column("characters", "owner_user_id")
