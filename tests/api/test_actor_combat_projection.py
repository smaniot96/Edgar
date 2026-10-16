"""The focused HUD must use engine actor identities even when earned labels collide."""

from apps.api.services.turn_runner import combat_state_public


def test_public_combat_retains_authoritative_current_actor_with_duplicate_display_names():
    state = {
        "round": 2,
        "ended": False,
        "current_turn": "Mara",
        "current_turn_index": 1,
        "initiative": [{"id": "npc-mara", "name": "Mara"}, {"id": "player", "name": "Mara"}],
        "combatants": [
            {"name": "npc-mara", "display_name": "Mara", "is_player": False},
            {"name": "player", "display_name": "Mara", "is_player": True},
        ],
    }
    public = combat_state_public(state)
    assert [actor["id"] for actor in public["combatants"]] == ["npc-mara", "player"]
    assert public["current_turn_id"] == "player"
    assert public["current_turn"] == "Mara"
    assert combat_state_public({**state, "ended": True})["current_turn_id"] is None
