"""Stored roll records for reload (EDG-030). Pure: no database, so these run locally."""

from __future__ import annotations

from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from apps.api.services.roll_record import (
    ROLL_CAPTURED,
    ROLL_NO_ROLL,
    ROLL_NOT_CAPTURED,
    build_roll_record,
    roll_status_for,
    stored_roll,
)


def _advantage_ruling() -> AdjudicationResult:
    return AdjudicationResult(
        success=True,
        damage=None,
        mechanical_summary="Stealth: 1d20+3 [14, 7] = 17 vs DC 15.",
        check="stealth",
        dc=15,
        against="DC",
        modifier=3,
        roll_expression="1d20+3",
        natural_roll=14,
        dice_result=17,
        dice_log=[
            {
                "expression": "1d20",
                "rolls": [14, 7],
                "modifier": 0,
                "total": 14,
                "purpose": "stealth",
                "roll_mode": "advantage",
                "kept": 14,
            }
        ],
        citations=[{"reference_id": "PHB-p1", "source": "PHB", "source_sha256": "a" * 64}],
        citation_ids=["PHB-p1"],
        conditions=["prone", ""],
    )


def test_record_keeps_the_original_ruling_and_both_faces() -> None:
    roll = build_roll_record(_advantage_ruling(), "turn-1")

    assert roll is not None
    assert roll["turn_id"] == "turn-1"
    assert roll["check"] == "stealth"
    assert roll["dc"] == 15 and roll["against"] == "DC"
    assert roll["roll_expression"] == "1d20+3"
    assert roll["dice_result"] == 17 and roll["natural_roll"] == 14
    assert roll["success"] is True
    assert roll["conditions"] == ["prone"]
    assert roll["dice_log"] == [
        {
            "expression": "1d20",
            "rolls": [14, 7],
            "modifier": 0,
            "total": 14,
            "purpose": "stealth",
            "roll_mode": "advantage",
            "kept": 14,
        }
    ]


def test_record_is_a_whitelist_without_private_or_source_fields() -> None:
    roll = build_roll_record(_advantage_ruling(), "turn-1")

    assert roll is not None
    assert "citations" not in roll and "citation_ids" not in roll
    assert "story_effects" not in roll and "flags_set" not in roll and "scene_id" not in roll
    assert "a" * 64 not in str(roll)


def test_healing_and_damage_come_from_the_applied_numbers() -> None:
    healing = AdjudicationResult(
        success=True,
        dice_result=9,
        roll_expression="2d4+2",
        dice_log=[{"expression": "2d4+2", "rolls": [3, 2], "modifier": 2, "total": 7}],
        character_update=CharacterUpdate(hp_delta=7),
    )
    hurt = AdjudicationResult(success=False, damage=4, dice_result=None, dice_log=[{"total": 4}])

    assert build_roll_record(healing, "t")["healing"] == 7
    assert build_roll_record(healing, "t")["damage"] is None
    assert build_roll_record(hurt, "t")["damage"] == 4
    assert build_roll_record(hurt, "t")["healing"] is None


def test_turn_without_dice_stores_no_record() -> None:
    clarifying = AdjudicationResult(success=True, mechanical_summary="Which door?")

    assert build_roll_record(clarifying, "t") is None
    assert build_roll_record(None, "t") is None
    assert roll_status_for(None) == ROLL_NO_ROLL
    assert roll_status_for(build_roll_record(_advantage_ruling(), "t")) == ROLL_CAPTURED


def test_legacy_rows_without_a_status_are_not_captured() -> None:
    # Rows written before EDG-030 hold the narration only; nothing may be inferred for them.
    legacy = {"player_input": "I climb", "narration": "You climb.", "turn_id": "old"}

    assert stored_roll(legacy) == (ROLL_NOT_CAPTURED, None)
    assert stored_roll(None) == (ROLL_NOT_CAPTURED, None)


def test_stored_statuses_round_trip() -> None:
    roll = build_roll_record(_advantage_ruling(), "t")
    captured = {"roll_status": ROLL_CAPTURED, "roll": roll}

    assert stored_roll(captured) == (ROLL_CAPTURED, roll)
    assert stored_roll({"roll_status": ROLL_NO_ROLL, "roll": None}) == (ROLL_NO_ROLL, None)
    # A captured status without a record is a damaged row: never shown as a ruling.
    assert stored_roll({"roll_status": ROLL_CAPTURED, "roll": None}) == (ROLL_NOT_CAPTURED, None)


def test_rest_evidence_is_stored_with_the_record_and_survives_storage() -> None:
    from agent.models.adjudication import RestRecord

    rest = AdjudicationResult(
        success=True,
        mechanical_summary="",
        dice_log=[
            {
                "expression": "1d10+2",
                "rolls": [5],
                "modifier": 2,
                "total": 7,
                "purpose": "short rest hit die",
            }
        ],
        rest_record=RestRecord(
            kind="short",
            expression="1d10+2",
            rolled=7,
            healed=7,
            die="d10",
            hit_dice_remaining=1,
            hit_dice_total=2,
        ),
        character_update=CharacterUpdate(hp_delta=7, hit_dice_spent=1),
    )

    roll = build_roll_record(rest, "rest-1")

    assert roll is not None
    assert roll["healing"] == 7
    assert roll["rest"] == {
        "kind": "short",
        "refused": False,
        "expression": "1d10+2",
        "rolled": 7,
        "healed": 7,
        "die": "d10",
        "hit_dice_remaining": 1,
        "hit_dice_total": 2,
        "hit_dice_recovered": None,
    }
    assert roll["dice_log"][0]["purpose"] == "short rest hit die"
    # Storage keeps the record as a plain JSON object and reads it back unchanged.
    stored = {"roll_status": roll_status_for(roll), "roll": roll}
    assert stored_roll(stored) == (ROLL_CAPTURED, roll)


def test_refused_short_rest_with_no_dice_still_stores_its_evidence() -> None:
    from agent.models.adjudication import RestRecord

    refused = AdjudicationResult(
        success=True,
        mechanical_summary="Eda has no Hit Dice left until a long rest. No healing is rolled.",
        rest_record=RestRecord(
            kind="short",
            refused=True,
            die="d10",
            healed=0,
            hit_dice_remaining=0,
            hit_dice_total=2,
        ),
    )

    roll = build_roll_record(refused, "rest-2")

    assert roll is not None and roll["dice_result"] is None and roll["dice_log"] == []
    assert roll["rest"]["refused"] is True
    assert (roll["rest"]["hit_dice_remaining"], roll["rest"]["hit_dice_total"]) == (0, 2)
    assert roll_status_for(roll) == ROLL_CAPTURED
