"""EDG-034: the play payload exposes derived spell slot counts for casters only (no DB needed).

Totals come from class and level; `remaining` is total minus the stored expended count. Legacy
sheets with no expended key show full slots and nothing is rewritten.
"""

from types import SimpleNamespace

import pytest

from apps.api.services import character_assignments as assignments


def _assignment(character_class: str, level: int, stats: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        level=level,  # EDG-045: the assignment owns the level the engine reads
        milestones_pending=0,
        hp_current=5,
        hp_max=12,
        stats={} if stats is None else stats,
        inventory={"items": []},
        character=SimpleNamespace(id=1, name="Ilse", character_class=character_class, level=level),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        # Level 3 wizard: 4 first-level and 2 second-level slots.
        (
            {"spell_slots_expended": {"1": 3, "2": 1}},
            {"1": {"total": 4, "remaining": 1}, "2": {"total": 2, "remaining": 1}},
        ),
        # Legacy sheet without the expended key: everything is still available.
        ({}, {"1": {"total": 4, "remaining": 4}, "2": {"total": 2, "remaining": 2}}),
        # Over-expended (e.g. after a level change) is clamped to zero, never negative.
        (
            {"spell_slots_expended": {"1": 9}},
            {"1": {"total": 4, "remaining": 0}, "2": {"total": 2, "remaining": 2}},
        ),
    ],
)
async def test_caster_payload_exposes_remaining_slots_per_level(
    monkeypatch, stats, expected
) -> None:
    ass = _assignment("Wizard", 3, stats)

    async def load(_db, _character_id, _campaign_id):
        return ass

    monkeypatch.setattr(assignments, "load_active_assignment", load)

    payload = await assignments.character_play_state(None, 1, 1)
    assert payload["spell_slots"] == expected
    # The derived view is read-only: the stored stats are untouched.
    assert ass.stats == stats


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("character_class", "level"),
    [("Fighter", 3), ("Rogue", 5)],
)
async def test_non_casters_have_no_spell_slots(monkeypatch, character_class, level) -> None:
    ass = _assignment(character_class, level, {"conditions": []})

    async def load(_db, _character_id, _campaign_id):
        return ass

    monkeypatch.setattr(assignments, "load_active_assignment", load)

    payload = await assignments.character_play_state(None, 1, 1)
    assert "spell_slots" not in payload


def test_half_caster_totals_follow_the_half_caster_table() -> None:
    # Level 2 paladin: two first-level slots (half-caster table).
    view = assignments.spell_slots_status({"class": "Paladin", "level": 2, "stats": {}})
    assert view == {"1": {"total": 2, "remaining": 2}}


def test_assignment_history_row_carries_derived_resources_only_where_present() -> None:
    from datetime import UTC, datetime

    from apps.api.schemas.character import CharacterAssignmentHistoryRead

    def row(character_class: str, stats: dict) -> dict:
        return CharacterAssignmentHistoryRead(
            id=1,
            campaign_id=1,
            campaign_title="Mines",
            hp_current=5,
            hp_max=12,
            stats=stats,
            inventory={},
            assigned_at=datetime(2026, 10, 1, tzinfo=UTC),
            ended_at=None,
            status="active",
            **assignments.resource_view({"class": character_class, "level": 3, "stats": stats}),
        ).model_dump()

    caster = row("Wizard", {"spell_slots_expended": {"1": 1}})
    assert caster["spell_slots"] == {
        "1": {"total": 4, "remaining": 3},
        "2": {"total": 2, "remaining": 2},
    }
    assert caster["second_wind"] is None

    fighter = row("Fighter", {})
    assert fighter["second_wind"] == {"uses_remaining": 1, "uses_max": 1}
    assert fighter["spell_slots"] is None
