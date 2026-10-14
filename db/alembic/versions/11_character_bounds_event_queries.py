"""Enforce existing character bounds and index event-type/id lookups.

Revision ID: 11_character_bounds
Revises: 10_integrity
Create Date: 2026-10-14

These bounds match the public character and assignment schemas. Upgrade refuses invalid
historical rows with counts and remediation guidance; it never clamps or deletes player data.
Downgrade removes only the added constraints/index, preserving every row and value.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "11_character_bounds"
down_revision: str | Sequence[str] | None = "10_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS = (
    ("characters", "characters_level_valid", "level BETWEEN 1 AND 20"),
    ("characters", "characters_hp_max_positive", "hp_max >= 1"),
    ("character_assignments", "character_assignments_hp_nonnegative", "hp_current >= 0"),
    ("character_assignments", "character_assignments_hp_max_positive", "hp_max >= 1"),
)
_EVENT_INDEX = "ix_event_log_session_id_event_type_id"


def upgrade() -> None:
    bind = op.get_bind()
    invalid = []
    for table, name, expression in _CHECKS:
        count = bind.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE NOT ({expression})"))
        if count:
            invalid.append(f"{table}: {count} row(s) violate {name}")
    if invalid:
        raise RuntimeError(
            "Character bounds migration refused invalid existing data: "
            + "; ".join(invalid)
            + ". Review and correct these rows explicitly, then retry; no data was changed."
        )
    for table, name, expression in _CHECKS:
        op.create_check_constraint(name, table, expression)
    op.create_index(_EVENT_INDEX, "event_log", ["session_id", "event_type", "id"])


def downgrade() -> None:
    op.drop_index(_EVENT_INDEX, table_name="event_log")
    for table, name, _expression in reversed(_CHECKS):
        op.drop_constraint(name, table, type_="check")
