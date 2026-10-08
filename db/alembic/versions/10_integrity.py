"""referential integrity: ON DELETE rules, FK indexes, world_flags uniqueness, hp check

Revision ID: 10_integrity
Revises: 9_combat_combatants
Create Date: 2026-10-08

* Recreates the child -> parent foreign keys with ON DELETE rules so deleting a campaign or a
  session no longer fails on dependent rows:
    sessions.campaign_id, npcs.campaign_id, world_flags.campaign_id  -> CASCADE
    event_log.session_id, combat_state.session_id                    -> CASCADE
    sessions.active_character_id, combat_state.active_character      -> SET NULL
  (character_assignments already cascades since revision 8; users FKs stay RESTRICT.)
* Indexes every foreign-key column; event_log gets (session_id, created_at) for the
  chronological per-session reads.
* De-duplicates world_flags (keeps the newest row, i.e. highest id, per (campaign_id, key))
  and adds UNIQUE (campaign_id, key).
* Clamps character_assignments.hp_current to hp_max and adds CHECK hp_current <= hp_max.
* Gives sessions.started_at the server default now() the ORM model already declares.

Downgrade restores the original NO ACTION foreign keys and drops the new indexes and
constraints. De-duplicated flags and clamped HP are not restored (lossy, but harmless).
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "10_integrity"
down_revision: str | Sequence[str] | None = "9_combat_combatants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (table, column, referred table, ondelete)
_FKS: list[tuple[str, str, str, str]] = [
    ("sessions", "campaign_id", "campaigns", "CASCADE"),
    ("sessions", "active_character_id", "characters", "SET NULL"),
    ("npcs", "campaign_id", "campaigns", "CASCADE"),
    ("world_flags", "campaign_id", "campaigns", "CASCADE"),
    ("event_log", "session_id", "sessions", "CASCADE"),
    ("combat_state", "session_id", "sessions", "CASCADE"),
    ("combat_state", "active_character", "characters", "SET NULL"),
]

# (index name, table, columns) — names match SQLAlchemy's `index=True` convention.
_INDEXES: list[tuple[str, str, list[str]]] = [
    ("ix_campaigns_created_by", "campaigns", ["created_by"]),
    ("ix_characters_owner_user_id", "characters", ["owner_user_id"]),
    ("ix_character_assignments_character_id", "character_assignments", ["character_id"]),
    ("ix_character_assignments_campaign_id", "character_assignments", ["campaign_id"]),
    ("ix_sessions_campaign_id", "sessions", ["campaign_id"]),
    ("ix_sessions_active_character_id", "sessions", ["active_character_id"]),
    ("ix_npcs_campaign_id", "npcs", ["campaign_id"]),
    ("ix_combat_state_session_id", "combat_state", ["session_id"]),
    ("ix_combat_state_active_character", "combat_state", ["active_character"]),
    ("ix_event_log_session_id_created_at", "event_log", ["session_id", "created_at"]),
]


def _drop_fk(bind: Any, table: str, column: str) -> None:
    """Drop the FK on `table.column` whatever its (auto-generated) name is."""
    for fk in sa.inspect(bind).get_foreign_keys(table):
        if fk.get("constrained_columns") == [column] and fk.get("name"):
            op.drop_constraint(fk["name"], table, type_="foreignkey")


def _recreate_fks(ondelete_for: dict[tuple[str, str], str | None]) -> None:
    bind = op.get_bind()
    for table, column, referred, _ in _FKS:
        _drop_fk(bind, table, column)
        op.create_foreign_key(
            f"{table}_{column}_fkey",
            table,
            referred,
            [column],
            ["id"],
            ondelete=ondelete_for[(table, column)],
        )


def upgrade() -> None:
    _recreate_fks({(t, c): od for t, c, _, od in _FKS})

    for name, table, cols in _INDEXES:
        op.create_index(name, table, cols)

    # Keep the newest flag per (campaign_id, key) before enforcing uniqueness.
    op.execute(
        """
        DELETE FROM world_flags wf
        USING world_flags newer
        WHERE wf.campaign_id = newer.campaign_id
          AND wf.key = newer.key
          AND wf.id < newer.id
        """
    )
    op.create_unique_constraint(
        "uq_world_flags_campaign_id_key", "world_flags", ["campaign_id", "key"]
    )

    op.execute("UPDATE character_assignments SET hp_current = hp_max WHERE hp_current > hp_max")
    op.create_check_constraint(
        "character_assignments_hp_within_max",
        "character_assignments",
        "hp_current <= hp_max",
    )

    op.alter_column(
        "sessions",
        "started_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    op.alter_column(
        "sessions",
        "started_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=None,
    )
    op.drop_constraint(
        "character_assignments_hp_within_max", "character_assignments", type_="check"
    )
    op.drop_constraint("uq_world_flags_campaign_id_key", "world_flags", type_="unique")
    for name, table, _ in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
    _recreate_fks({(t, c): None for t, c, _, _ in _FKS})
