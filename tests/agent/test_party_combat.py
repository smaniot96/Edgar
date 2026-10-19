"""EDG-049: one combat across several human-controlled heroes, with scripted dice.

Every die comes from `ScriptedDice`, so each turn order, target choice and death save is exact. Solo and
single-hero fights are covered by the existing combat suites (and the round golden), which this ticket
leaves unchanged.
"""

from __future__ import annotations

import copy

import pytest

from agent.combat_view import sort_by_initiative
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.party_combat import holder_character_id, is_party
from agent.resolution import DEATH_SAVES_KEY
from tests.agent.test_combat import FIGHTER, _enemy
from tests.agent.test_spells import CLERIC, WIZARD, ScriptedDice
from tools.dice import use_rng


def _hero(sheet: dict, init: int, *, hp: int | None = None, bonus: int = 1, **extra) -> dict:
    return {
        "name": f"pc-{sheet['id']}",
        "display_name": sheet["name"],
        "character_id": sheet["id"],
        "is_player": True,
        "hp_current": sheet["hp_current"] if hp is None else hp,
        "hp_max": sheet["hp_max"],
        "ac": 14,
        "alive": True,
        "disposition": "active" if (hp is None or hp > 0) else "unconscious",
        "init": init,
        "init_bonus": bonus,
        **extra,
    }


def _combat(rows: list[dict], *, cursor: int, round_num: int = 1) -> dict:
    ordered = sort_by_initiative(rows)
    return {
        "id": 7,
        "round": round_num,
        "ended": False,
        "combatants": ordered,
        "current_turn_index": cursor,
    }


def _state(combat: dict | None, sheet: dict, message: str, *, party=None, **extra) -> dict:
    state = {
        "player_input": message,
        "session_id": 1,
        "campaign_id": 1,
        "messages": [],
        "character": dict(sheet),
        "party": [dict(s) for s in (party if party is not None else [FIGHTER, WIZARD])],
        "world_flags": {},
        "npcs": [],
        "adventure_collections": [],
        "parsed_input": ParsedInput(intent="combat", check="attack", target="goblin"),
    }
    if combat is not None:
        state["combat_state"] = combat
    state.update(extra)
    return state


def _two_heroes_one_goblin(*, goblin_init: int = 5, goblin_hp: int = 30) -> dict:
    # Eda (init 15) -> Ilse (init 10) -> Goblin: round order is fixed by the persisted rows.
    return _combat(
        [_hero(FIGHTER, 15), _hero(WIZARD, 10, bonus=2), _enemy("Goblin", hp=goblin_hp, init=goblin_init)],
        cursor=0,
    )


@pytest.fixture
def adj(fake_llm_schema, fake_retrieval):
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    return fake_llm_schema


def _by(out: dict, name: str) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c["name"] == name)


# --- initiative order, ties ---------------------------------------------------------------------


def test_initiative_order_is_shared_by_heroes_and_foes_and_ties_are_stable() -> None:
    rows = [
        _hero(WIZARD, 12, bonus=2),
        _enemy("Goblin", init=12),
        _hero(FIGHTER, 12, bonus=2),
        _hero(CLERIC, 12, bonus=2),
    ]
    ordered = sort_by_initiative(rows)
    # Same total and bonus: heroes beat the foe, and tied heroes go in character order.
    assert [c["name"] for c in ordered] == ["pc-1", "pc-2", "pc-3", "Goblin"]
    assert is_party(ordered)


@pytest.mark.asyncio
async def test_a_party_fight_opens_with_one_initiative_order_for_all_heroes(adj, monkeypatch) -> None:
    async def roster(_state, _targets, *, budget_summary=None, party_size=1):
        assert party_size == 2
        return [_enemy("Goblin", init=5)]

    monkeypatch.setattr("agent.graph_combat._build_enemy_roster", roster)
    # Eda d20=9 (+1) = 10, Ilse d20=10 (+2) = 12, Goblin d20=4 (+1) = 5.
    state = _state(None, FIGHTER, "I attack the goblin")
    with use_rng(ScriptedDice([9, 10, 4])):
        out = await combat_round_node(state)
    cs = out["combat_state"]
    assert [c["name"] for c in cs["combatants"]] == ["pc-2", "pc-1", "Goblin"]
    # Ilse is first: the opener's declaration is not rolled, the engine says whose turn it is.
    assert holder_character_id(cs) == 2 and cs["current_turn"] == "Ilse"
    assert cs["current_turn_index"] == 0 and cs["round"] == 1
    assert "Ilse is up first" in out["narration"] and "waits" in out["narration"]
    assert out["party_updates"] == []
    assert [e["character_id"] for e in cs["initiative"] if "character_id" in e] == [2, 1]


# --- per-hero turns, enforced by the engine -----------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_hero_whose_slot_is_next_acts_and_nothing_changes_otherwise(adj) -> None:
    combat = _two_heroes_one_goblin()
    before = copy.deepcopy(combat)
    with use_rng(ScriptedDice([])):  # no die may be rolled
        out = await combat_round_node(_state(combat, WIZARD, "I attack the goblin"))
    assert "It is Eda's turn" in out["narration"]
    assert out["combat_state"]["combatants"] == before["combatants"]
    assert out["combat_state"]["round"] == 1


@pytest.mark.asyncio
async def test_each_hero_acts_on_its_own_turn_with_its_own_action_economy(adj) -> None:
    combat = _two_heroes_one_goblin()
    # Eda: attack d20=15 (hits AC 12), damage d8=4. Then Ilse's slot pauses the request.
    with use_rng(ScriptedDice([15, 4])):
        first = await combat_round_node(_state(combat, FIGHTER, "I attack the goblin"))
    cs = first["combat_state"]
    assert holder_character_id(cs) == 2 and cs["round"] == 1
    assert _by(first, "pc-1")["action_economy"]["action"] == "attack"
    assert "action_economy" not in _by(first, "pc-2")
    assert _by(first, "Goblin")["hp_current"] < 30
    # Ilse dodges: her own economy shows it, Eda's record is untouched. The goblin then acts: two
    # heroes stand, so one die (1d2) chooses its target; it picks Ilse (2); she is dodging, so it rolls two d20s (2 and 2) and misses.
    dodge = ParsedInput(intent="combat")
    with use_rng(ScriptedDice([2, 2, 2])):
        second = await combat_round_node(
            _state(copy.deepcopy(cs), WIZARD, "I take the Dodge action", parsed_input=dodge)
        )
    assert _by(second, "pc-2")["action_economy"]["dodging"] is True
    assert _by(second, "pc-1")["action_economy"]["action"] == "attack"
    assert "Goblin chooses a target" in [d["purpose"] for d in second["adjudication_result"].dice_log]
    cs2 = second["combat_state"]
    assert cs2["round"] == 2 and holder_character_id(cs2) == 1
    assert "Eda is up next" in second["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
async def test_foes_choose_among_all_standing_heroes_and_damage_the_chosen_one(adj) -> None:
    combat = _combat(
        [_hero(FIGHTER, 15), _hero(WIZARD, 10, bonus=2), _enemy("Goblin", init=12)], cursor=1
    )
    # Order: Eda 15, Goblin 12, Ilse 10. Cursor on Eda would be 0; put Eda's turn first.
    combat["current_turn_index"] = 0
    with use_rng(ScriptedDice([15, 3, 1, 15, 4])):
        # Eda hits (d20=15, damage 3). The goblin picks 1d2=1, the first standing hero in initiative
        # order (Eda), hits with d20=15 against AC 14 and rolls 4+1 damage. Ilse's slot pauses.
        out = await combat_round_node(_state(combat, FIGHTER, "I attack the goblin"))
    purposes = [d["purpose"] for d in out["adjudication_result"].dice_log]
    assert "Goblin chooses a target" in purposes
    assert _by(out, "pc-1")["hp_current"] == 12 - 5 and _by(out, "pc-2")["hp_current"] == 8
    assert holder_character_id(out["combat_state"]) == 2


# --- death saves, per hero ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_downed_hero_rolls_its_own_death_save_while_another_acts(adj) -> None:
    down = _hero(WIZARD, 10, hp=0, bonus=2, **{DEATH_SAVES_KEY: {"state": "dying", "successes": 0, "failures": 0}})
    combat = _combat([_hero(FIGHTER, 15), down, _enemy("Goblin", init=5, hp=30)], cursor=0)
    wizard_down = {**WIZARD, "hp_current": 0}
    # Eda attacks (d20=15, damage 4). Ilse's slot is automatic: death save d20=12 succeeds. The
    # goblin (init 5) idles against no standing hero except Eda: d20=1 misses. Round 2 pauses at Eda.
    with use_rng(ScriptedDice([15, 4, 12, 1])):
        out = await combat_round_node(
            _state(combat, FIGHTER, "I attack the goblin", party=[FIGHTER, wizard_down], death_mode="death_saves")
        )
    ilse = _by(out, "pc-2")
    assert ilse[DEATH_SAVES_KEY]["successes"] == 1 and ilse["hp_current"] == 0
    purposes = [d["purpose"] for d in out["adjudication_result"].dice_log]
    assert "Ilse death saving throw" in purposes
    assert out["combat_state"]["round"] == 2
    assert holder_character_id(out["combat_state"]) == 1
    updates = {u["character_id"]: u for u in out["party_updates"]}
    assert updates[2]["death_saves"]["successes"] == 1 and updates[2]["hp_delta"] == 0


@pytest.mark.asyncio
async def test_an_ally_heals_a_downed_hero_back_into_the_fight(adj) -> None:
    down = _hero(FIGHTER, 15, hp=0, **{DEATH_SAVES_KEY: {"state": "dying", "successes": 1, "failures": 1}})
    combat = _combat([down, _hero(CLERIC, 12, bonus=1), _enemy("Goblin", init=1)], cursor=1)
    fighter_down = {**FIGHTER, "hp_current": 0}
    parsed = ParsedInput(intent="combat", target="Eda")
    # Dara casts Cure Wounds on Eda: 1d8=5 (+3 WIS) = 8 HP. The goblin then acts against a standing hero
    # (only Dara stands, no target die): d20=1 misses. Round 2: Eda is up.
    with use_rng(ScriptedDice([5, 1, 1])):
        out = await combat_round_node(
            _state(
                combat,
                CLERIC,
                "I cast Cure Wounds on Eda",
                party=[CLERIC, fighter_down],
                parsed_input=parsed,
                death_mode="death_saves",
            )
        )
    eda = _by(out, "pc-1")
    assert eda["hp_current"] == 8 and eda[DEATH_SAVES_KEY] is None and eda["disposition"] == "active"
    assert "Eda regains 8 HP and is conscious again" in out["adjudication_result"].mechanical_summary
    updates = {u["character_id"]: u for u in out["party_updates"]}
    assert updates[1] == {"character_id": 1, "hp_delta": 8, "death_saves": {}}


# --- end conditions -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_fight_is_won_when_the_last_foe_falls_and_ends_for_everyone(adj) -> None:
    combat = _combat([_hero(FIGHTER, 15), _hero(WIZARD, 10, bonus=2), _enemy("Goblin", init=5, hp=1, ac=1)], cursor=0)
    with use_rng(ScriptedDice([15, 6])):
        out = await combat_round_node(_state(combat, FIGHTER, "I attack the goblin"))
    cs = out["combat_state"]
    assert cs["ended"] and cs["outcome"] == "victory" and cs["current_turn"] is None
    assert cs["current_turn_character_id"] is None


@pytest.mark.asyncio
async def test_when_every_hero_is_down_the_party_is_defeated_and_left_at_one_hp(adj) -> None:
    # Subdued mode: Eda (hp 1) is the last standing hero; the goblin (init 20) downs her.
    rows = [_hero(FIGHTER, 5, hp=1), _hero(WIZARD, 4, hp=0, bonus=2), _enemy("Goblin", init=20, hp=30)]
    combat = _combat(rows, cursor=1)
    wizard_down = {**WIZARD, "hp_current": 0}
    fighter_low = {**FIGHTER, "hp_current": 1}
    # cursor 1 is Eda (order Goblin 20, Eda 5, Ilse 4). Eda attacks (d20=2 misses), Ilse (down) is
    # skipped, the round wraps, the goblin hits Eda: d20=20, damage 6 -> Eda at 0, and nobody stands.
    with use_rng(ScriptedDice([2, 20, 6, 6])):
        out = await combat_round_node(
            _state(combat, fighter_low, "I attack the goblin", party=[fighter_low, wizard_down])
        )
    cs = out["combat_state"]
    assert cs["ended"] and cs["outcome"] == "defeat"
    assert _by(out, "pc-1")["hp_current"] == 1 and _by(out, "pc-2")["hp_current"] == 1
    assert "Every hero is down" in out["adjudication_result"].mechanical_summary
    assert {u["character_id"] for u in out["party_updates"]} == {2}


@pytest.mark.asyncio
async def test_one_hero_fleeing_leaves_the_fight_to_the_others(adj) -> None:
    combat = _two_heroes_one_goblin()
    with use_rng(ScriptedDice([])):
        out = await combat_round_node(
            _state(combat, FIGHTER, "I flee!", parsed_input=ParsedInput(intent="combat"))
        )
    cs = out["combat_state"]
    assert not cs["ended"]
    assert _by(out, "pc-1")["fled"] is True
    assert holder_character_id(cs) == 2
