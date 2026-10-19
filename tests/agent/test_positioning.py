"""EDG-044 (second slice): coarse positioning, movement, Dash, Disengage and opportunity attacks.

Foes stand in a melee or ranged band from the hero. Movement is free and declared in the hero's own
words; one band change per turn, two with Dash. Leaving a foe's melee band without Disengage
provokes one opportunity attack, made with that foe's reaction. Dice are engine-rolled and seeded;
the tests assert what the engine recorded, never a particular roll.
"""

import json
import random

import pytest

from agent.action_economy import dash_declared, disengage_declared
from agent.combatants import add_effect, attack_roll_mode
from agent.graph_combat import combat_round_node, is_flee
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import MonsterAttack, MonsterStats, combat_fields, stats_of
from agent.positioning import (
    MELEE,
    RANGED,
    attacks_from_band,
    band_of,
    initial_band,
    move_refusal,
    movement_steps,
    plan_moves,
    reach_refusal,
)
from agent.supported_rules import (
    SUPPORTED_MECHANICS,
    UNSUPPORTED_MECHANICS,
    EngineApplied,
    apply_narration_boundary,
    classify_unsupported_actions,
)
from apps.api.services.turn_runner import combat_state_public
from tests.agent.confirming import confirm_turn
from tests.agent.test_combat import _enemy, _player, _state
from tools.dice import use_rng

HERO = "Eda"

FIGHTER = _state([], "")["character"]
ARCHER_HERO = {**FIGHTER, "inventory": {"weapons": ["longbow"], "armor": "chain mail"}}


def _brute(name: str, *, init: int, band: str | None = None) -> dict:
    """A melee-only exact record: its one listed attack is a melee attack."""
    stats = MonsterStats(
        name=name,
        hp_max=18,
        ac=12,
        abilities={"DEX": 1},
        attacks=(MonsterAttack("Club", 3, "1d6+1", "bludgeoning"),),
    )
    row = {
        "name": name,
        "is_player": False,
        "disposition": "active",
        "alive": True,
        "init": init,
        "init_bonus": 1,
        "monster_stats": stats.to_dict(),
        **combat_fields(stats),
    }
    if band is not None:
        row["band"] = band
    return row


def _archer(name: str, *, init: int, band: str | None = None) -> dict:
    """A ranged-only exact record: every attack it lists is a ranged attack."""
    stats = MonsterStats(
        name=name,
        hp_max=12,
        ac=12,
        abilities={"DEX": 1},
        attacks=(MonsterAttack("Shortbow", 3, "1d6+1", "piercing", ranged=True),),
    )
    row = {
        "name": name,
        "is_player": False,
        "disposition": "active",
        "alive": True,
        "init": init,
        "init_bonus": 1,
        "monster_stats": stats.to_dict(),
        **combat_fields(stats),
    }
    if band is not None:
        row["band"] = band
    return row


async def _turn(
    fake,
    combatants,
    message,
    *,
    character=None,
    parsed=None,
    seed=0,
) -> dict:
    fake.responses[ParsedInput] = parsed or ParsedInput(
        intent="combat", check="attack", target="goblin"
    )
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(combatants, message)
    if character is not None:
        state["character"] = character
    with use_rng(random.Random(seed)):
        out = await combat_round_node(state)
        return await confirm_turn(out, state)


def _entries(out: dict, purpose: str) -> list[dict]:
    return [d for d in out["adjudication_result"].dice_log if d.get("purpose") == purpose]


def _foe(out: dict, name: str) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c["name"] == name)


def _hero(out: dict) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c.get("is_player"))


def _hero_row(init: int = 20) -> dict:
    return {**_player(init=init, ac=13), "display_name": HERO}


# --- pure: declared movement and Dash ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "steps"),
    [
        ("I back away", [RANGED]),
        ("I retreat to range", [RANGED]),
        ("I close in on the archer", [MELEE]),
        ("I charge the goblin", [MELEE]),
        ("I back away from the goblin and close in on the archer", [RANGED, MELEE]),
        ("I dash to the archer", [MELEE]),
        # Idioms and ordinary speech are not movement.
        ("dash of salt", []),
        ("I dash a glance at the door", []),
        ("I charge my sword", []),
        ("I attack the goblin", []),
    ],
)
def test_movement_is_read_from_the_declared_clauses(text, steps) -> None:
    assert [band for band, _ in movement_steps(text)] == steps


@pytest.mark.parametrize(
    ("text", "dash", "disengage"),
    [
        ("I dash toward the goblin", True, False),
        ("I take the dash action", True, False),
        ("I disengage and back away", False, True),
        ("I take the Disengage action", False, True),
        ("dash of salt", False, False),
        ("I dash a glance", False, False),
        ("I dash my hopes on the door", False, False),
        ("The goblin disengages", False, False),
    ],
)
def test_dash_and_disengage_are_declared_only_in_the_players_clause(text, dash, disengage) -> None:
    assert dash_declared(text) is dash
    assert disengage_declared(text) is disengage


def test_hero_moves_one_band_per_turn_and_dash_allows_two() -> None:
    foes = [_brute("Goblin", init=1, band=MELEE), _archer("Archer", init=2, band=RANGED)]
    one = plan_moves("I back away from the goblin", ["Goblin"], foes, pick=_pick(foes))
    assert [band for band, _ in one.steps] == [RANGED] and one.question is None
    assert move_refusal(HERO, 1, dash=False) is None

    two = plan_moves(
        "I back away from the goblin and close in on the archer",
        ["Goblin", "Archer"],
        foes,
        pick=_pick(foes),
    )
    assert len(two.steps) == 2
    assert move_refusal(HERO, 2, dash=False) is not None
    assert "Dash" in move_refusal(HERO, 2, dash=False)
    assert move_refusal(HERO, 2, dash=True) is None
    assert move_refusal(HERO, 3, dash=True) is not None
    # Planning changes no row: only the planned bands on the copy move.
    assert [band_of(f) for f in foes] == [MELEE, RANGED]


def test_planning_leaves_the_rows_untouched_and_names_the_foe_when_ambiguous() -> None:
    foes = [_archer("Archer", init=1, band=RANGED), _archer("Archer 2", init=2, band=RANGED)]
    ambiguous = plan_moves("I close in", [], foes, pick=_pick(foes))
    assert (
        ambiguous.question and "Archer" in ambiguous.question and "Archer 2" in ambiguous.question
    )
    assert ambiguous.steps == []
    assert [band_of(f) for f in foes] == [RANGED, RANGED]


def _pick(foes):
    def pick(name: str):
        return next((f for f in foes if f["name"].lower() == name.lower()), None)

    return pick


def test_initial_band_follows_the_record_and_legacy_rows_read_as_melee() -> None:
    melee_record = _brute("Goblin", init=1).get("monster_stats")
    assert initial_band(stats_of({"monster_stats": melee_record})) == MELEE
    ranged_record = _archer("Archer", init=1)["monster_stats"]
    assert initial_band(stats_of({"monster_stats": ranged_record})) == RANGED
    assert initial_band(None) == MELEE  # a legacy or generic foe
    assert band_of({"name": "Goblin"}) == MELEE  # a row with no band key
    assert band_of({"band": RANGED}) == RANGED


def test_melee_only_foe_has_no_attack_from_range_and_archer_has_no_melee_attack() -> None:
    club = MonsterAttack("Club", 3, "1d6+1", "bludgeoning")
    bow = MonsterAttack("Shortbow", 3, "1d6+1", "piercing", ranged=True)
    assert attacks_from_band((club,), RANGED) == ()
    assert attacks_from_band((club, bow), RANGED) == (bow,)
    assert attacks_from_band((club, bow), MELEE) == (club,)
    assert attacks_from_band((bow,), MELEE) == (bow,)  # no melee attack: it shoots at disadvantage


def test_ranged_attack_within_five_feet_has_disadvantage_by_its_typed_source() -> None:
    hero = {"name": "player", "is_player": True}
    goblin = _brute("Goblin", init=1)
    assert attack_roll_mode(goblin, hero, ranged=True) == "normal"
    assert attack_roll_mode(goblin, hero, ranged=True, within_five_feet=True) == "disadvantage"
    assert (
        attack_roll_mode(goblin, hero, within_five_feet=True) == "normal"
    )  # melee is not affected


@pytest.mark.parametrize(
    ("text", "flees"),
    [
        ("I disengage and back away from the goblin", False),
        ("I retreat to range", False),
        ("I retreat from the goblin", False),
        ("We retreat back down the corridor.", True),
        ("I flee", True),
        ("I run away", True),
    ],
)
def test_disengage_and_retreat_to_range_are_moves_not_leaving_the_fight(text, flees) -> None:
    assert is_flee(text) is flees


def test_reach_refusal_names_the_move_that_fixes_it() -> None:
    text = reach_refusal(HERO, "Goblin")
    assert "cannot reach Goblin" in text and "close in on Goblin" in text


# --- the combat round ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_encounter_starts_each_foe_in_the_band_its_record_gives(
    fake_llm_schema, fake_retrieval, monkeypatch
) -> None:
    import agent.graph_combat as graph_combat

    async def roster(state, targets, *, budget_summary=None):  # the exact records, no LLM or index
        return [_brute("Goblin", init=1), _archer("Archer", init=2)]

    monkeypatch.setattr(graph_combat, "_build_enemy_roster", roster)
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(None, "I take the dodge action")
    state["combat_state"] = {"id": 7, "round": 1, "ended": False, "combatants": []}
    with use_rng(random.Random(1)):
        out = await combat_round_node(state)
    assert _foe(out, "Goblin")["band"] == MELEE
    assert _foe(out, "Archer")["band"] == RANGED


@pytest.mark.asyncio
async def test_bands_are_set_from_the_records_and_survive_a_reload(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_hero_row(), _brute("Goblin", init=1), _archer("Archer", init=2, band=RANGED)],
        "I take the dodge action",
    )
    assert _foe(out, "Archer")["band"] == RANGED
    # A row written before positioning has no band: it reads as melee and is not rewritten.
    assert band_of(_foe(out, "Goblin")) == MELEE and "band" not in _foe(out, "Goblin")
    stored = json.loads(json.dumps(out["combat_state"]["combatants"]))
    payload = combat_state_public({**out["combat_state"], "combatants": stored})
    bands = {c["name"]: c["band"] for c in payload["combatants"] if not c["is_player"]}
    assert bands == {"Goblin": MELEE, "Archer": RANGED}
    assert {c["name"]: c["reaction_used"] for c in payload["combatants"]}["Goblin"] is False


@pytest.mark.asyncio
async def test_moving_twice_without_dash_is_refused_with_no_dice(
    fake_llm_schema, fake_retrieval
) -> None:
    foes = [_brute("Goblin", init=1, band=MELEE)]
    out = await _turn(
        fake_llm_schema,
        [_hero_row(), *foes],
        "I back away from the goblin and close in on the goblin",
    )
    assert "change position once per turn" in out["narration"]
    assert out["adjudication_result"].dice_log == []
    assert out["combat_state"]["round"] == 1
    assert _foe(out, "Goblin")["band"] == MELEE
    assert "economy" not in out["combat_state"]


@pytest.mark.asyncio
async def test_dash_allows_the_second_move_and_provokes_the_reaction(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [
            _hero_row(),
            _brute("Goblin", init=1, band=MELEE),
            _archer("Archer", init=2, band=RANGED),
        ],
        "I dash away from the goblin, then close in on the archer",
        seed=3,
    )
    assert "change position" not in out["narration"]
    assert _foe(out, "Archer")["band"] == MELEE
    assert out["combat_state"]["economy"]["action"] == "Dash"
    assert _entries(out, "Goblin opportunity attack"), "leaving melee did not provoke"
    # The goblin acts after the hero: at range it spends its movement to close in again.
    assert "Goblin closes to melee range." in out["adjudication_result"].mechanical_summary
    assert _foe(out, "Goblin")["band"] == MELEE


@pytest.mark.asyncio
async def test_a_foe_at_range_closes_in_and_then_attacks(fake_llm_schema, fake_retrieval) -> None:
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=RANGED)],
        "I take the dodge action",
        seed=4,
    )
    assert _foe(out, "Goblin")["band"] == MELEE
    assert "Goblin closes to melee range." in out["adjudication_result"].mechanical_summary
    assert _entries(out, "Goblin Club attack"), "the foe did not attack after closing in"


@pytest.mark.asyncio
async def test_a_ranged_only_foe_at_range_stays_and_shoots(fake_llm_schema, fake_retrieval) -> None:
    out = await _turn(
        fake_llm_schema,
        [
            _hero_row(init=20),
            _brute("Goblin", init=2, band=MELEE),
            _archer("Archer", init=1, band=RANGED),
        ],
        "I attack the goblin",
        seed=5,
    )
    assert _foe(out, "Archer")["band"] == RANGED
    assert "closes to melee range" not in out["adjudication_result"].mechanical_summary
    assert _entries(out, "Archer Shortbow attack")
    assert "roll_mode" not in _entries(out, "Archer Shortbow attack")[0]


@pytest.mark.asyncio
async def test_standing_up_clears_hero_prone_without_spending_an_action(
    fake_llm_schema, fake_retrieval
) -> None:
    hero = _hero_row(init=20)
    add_effect(hero, "prone", source="Wing Attack", round_applied=1)
    out = await _turn(
        fake_llm_schema,
        [hero, _brute("Goblin", init=1, band=MELEE)],
        "I stand up and attack the goblin",
        seed=6,
    )

    assert not any(effect["kind"] == "prone" for effect in _hero(out).get("effects", []))
    assert "Eda stands up and is no longer prone." in out["adjudication_result"].mechanical_summary
    assert _entries(out, "attack check")[0].get("roll_mode", "normal") == "normal"
    assert out["combat_state"]["economy"]["action"] == "attack"


@pytest.mark.asyncio
async def test_a_ranged_foe_in_melee_shoots_at_disadvantage(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [
            _hero_row(init=20),
            _brute("Goblin", init=2, band=MELEE),
            _archer("Archer", init=1, band=MELEE),
        ],
        "I attack the goblin",
        seed=5,
    )
    assert _entries(out, "Archer Shortbow attack")[0]["roll_mode"] == "disadvantage"


@pytest.mark.asyncio
async def test_leaving_melee_provokes_one_opportunity_attack(
    fake_llm_schema, fake_retrieval
) -> None:
    # The goblin acts before the hero, so its reaction is spent on the hero's turn, not restored.
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=10), _brute("Goblin", init=20, band=MELEE)],
        "I back away from the goblin",
        seed=6,
    )
    opportunity = _entries(out, "Goblin opportunity attack")
    assert len(opportunity) == 1
    assert _foe(out, "Goblin")["band"] == RANGED
    # Its reaction is spent until its own next turn begins (the goblin already acted this round).
    assert _foe(out, "Goblin")["reaction_used"] is True
    assert "(opportunity attack)" in out["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
async def test_disengage_stops_the_opportunity_attack(fake_llm_schema, fake_retrieval) -> None:
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=10), _brute("Goblin", init=20, band=MELEE)],
        "I disengage and back away from the goblin",
        seed=6,
    )
    assert _entries(out, "Goblin opportunity attack") == []
    assert _foe(out, "Goblin")["band"] == RANGED
    assert out["combat_state"]["economy"]["action"] == "Disengage"
    assert _foe(out, "Goblin").get("reaction_used") in (None, False)


@pytest.mark.asyncio
async def test_a_spent_reaction_is_not_used_twice_in_the_round(
    fake_llm_schema, fake_retrieval
) -> None:
    spent = _brute("Goblin", init=1, band=MELEE)
    spent["reaction_used"] = (
        True  # used earlier this round; it comes back only at the goblin's turn
    )
    out = await _turn(fake_llm_schema, [_hero_row(init=20), spent], "I back away from the goblin")
    assert _entries(out, "Goblin opportunity attack") == []
    # Its own turn then restores the reaction, and it closes in again as a melee-only foe.
    assert _foe(out, "Goblin")["reaction_used"] is False
    assert "Goblin closes to melee range." in out["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
async def test_a_foe_without_a_melee_attack_makes_no_opportunity_attack(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=10), _archer("Archer", init=20, band=MELEE)],
        "I back away from the archer",
        seed=7,
    )
    assert _entries(out, "Archer opportunity attack") == []
    assert _foe(out, "Archer")["band"] == RANGED


@pytest.mark.asyncio
async def test_a_legacy_foe_without_a_band_is_in_melee(fake_llm_schema, fake_retrieval) -> None:
    legacy = _enemy("Goblin", hp=30, ac=1, init=20)  # no band key, no monster_stats
    out = await _turn(
        fake_llm_schema, [_hero_row(init=10), legacy], "I back away from the goblin", seed=8
    )
    assert _entries(out, "Goblin opportunity attack")
    assert _foe(out, "Goblin")["band"] == RANGED


@pytest.mark.asyncio
async def test_similarly_named_foes_move_and_provoke_separately(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [
            _hero_row(init=10),
            _brute("Goblin", init=20, band=MELEE),
            _brute("Goblin 2", init=21, band=MELEE),
        ],
        "I back away from the goblin 2",
        parsed=ParsedInput(intent="combat", check="attack", target="Goblin 2"),
        seed=9,
    )
    assert _foe(out, "Goblin 2")["band"] == RANGED
    assert _foe(out, "Goblin")["band"] == MELEE
    assert _entries(out, "Goblin 2 opportunity attack")
    assert _entries(out, "Goblin opportunity attack") == []


@pytest.mark.asyncio
async def test_a_melee_attack_at_range_is_refused_with_no_dice(
    fake_llm_schema, fake_retrieval
) -> None:
    fighter = {**FIGHTER, "hp_current": 12}
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=RANGED)],
        "I attack the goblin",
        character=fighter,
        parsed=ParsedInput(intent="combat", check="attack", target="goblin"),
    )
    assert "cannot reach Goblin" in out["narration"]
    assert out["adjudication_result"].dice_log == []
    assert out["combat_state"]["round"] == 1
    assert _foe(out, "Goblin")["hp_current"] == _foe(out, "Goblin")["hp_max"]


@pytest.mark.asyncio
async def test_closing_in_first_makes_the_melee_attack_possible(
    fake_llm_schema, fake_retrieval
) -> None:
    fighter = {**FIGHTER, "hp_current": 12}
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=RANGED)],
        "I close in on the goblin and attack the goblin",
        character=fighter,
        parsed=ParsedInput(intent="combat", check="attack", target="goblin"),
        seed=10,
    )
    assert _foe(out, "Goblin")["band"] == MELEE
    assert _entries(out, "attack check"), "the attack was not rolled after closing in"


@pytest.mark.asyncio
async def test_a_hero_ranged_weapon_in_melee_has_disadvantage_and_at_range_does_not(
    fake_llm_schema, fake_retrieval
) -> None:
    melee = await _turn(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=MELEE)],
        "I shoot the goblin",
        character=ARCHER_HERO,
        parsed=ParsedInput(intent="combat", check="attack", target="goblin"),
        seed=11,
    )
    assert _entries(melee, "attack check")[0]["roll_mode"] == "disadvantage"
    far = await _turn(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=RANGED)],
        "I shoot the goblin",
        character=ARCHER_HERO,
        parsed=ParsedInput(intent="combat", check="attack", target="goblin"),
        seed=11,
    )
    assert "roll_mode" not in _entries(far, "attack check")[0]


@pytest.mark.asyncio
async def test_band_and_reaction_survive_a_json_reload_in_the_public_payload(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=10), _brute("Goblin", init=20, band=MELEE)],
        "I back away from the goblin",
        seed=6,
    )
    stored = json.loads(json.dumps(out["combat_state"]["combatants"]))
    payload = combat_state_public({**out["combat_state"], "combatants": stored})
    goblin = next(c for c in payload["combatants"] if c["name"] == "Goblin")
    assert goblin["band"] == RANGED and goblin["reaction_used"] is True
    hero = next(c for c in payload["combatants"] if c["is_player"])
    assert hero["band"] is None


# --- contract --------------------------------------------------------------------------------


def test_positioning_moves_into_supported_and_the_remainder_is_narrowed() -> None:
    supported = {m.id: m for m in SUPPORTED_MECHANICS}
    unsupported = {m.id: m for m in UNSUPPORTED_MECHANICS}
    assert {"positioning", "opportunity_attacks", "action_economy"} <= set(supported)
    assert (
        "positioning_cover" in unsupported and unsupported["positioning_cover"].owner == "EDG-182"
    )
    assert "Help" in unsupported["action_economy_remainder"].title
    assert "Dash" not in unsupported["action_economy_remainder"].title
    assert "Disengage" not in unsupported["action_economy_remainder"].title
    assert "flanking" in unsupported["positioning_cover"].title.lower()


@pytest.mark.parametrize(
    ("text", "effects", "kinds"),
    [
        ("I take the dash action", (), ["action_economy"]),
        ("I take the dash action", ("dash",), []),
        ("I take the Disengage action", ("disengage",), []),
        ("I take the Disengage action", (), ["action_economy"]),
        ("I take the Help action", ("dash",), ["action_economy"]),
        ("I make an opportunity attack", ("opportunity_attack",), []),
        ("I make an opportunity attack", (), ["action_economy"]),
        ("I stand in cover", (), ["positioning"]),
    ],
)
def test_executed_dash_disengage_and_opportunity_attacks_are_not_declared(text, effects, kinds):
    got = classify_unsupported_actions(text, applied=EngineApplied(effects=effects))
    assert [a.kind for a in got] == kinds


def test_narration_of_an_executed_dash_survives_and_an_unexecuted_claim_is_struck() -> None:
    applied = EngineApplied(effects=("dash", "opportunity_attack"))
    actions = classify_unsupported_actions("I dash past the goblin", applied=applied)
    narration = "You dash past the goblin. The goblin makes an opportunity attack and misses."
    assert apply_narration_boundary(narration, actions, applied=applied) == narration
    stripped = apply_narration_boundary(
        "You take the Help action and aid the ally.",
        classify_unsupported_actions("I take the Help action"),
    )
    assert "You take the Help action" not in stripped and "Narrative ruling" in stripped


def test_narrator_roster_states_each_foes_band_and_legacy_rows_keep_their_text() -> None:
    from agent.combatants import narrator_roster

    banded = narrator_roster(
        [_brute("Goblin", init=1, band=MELEE), _archer("Archer", init=2, band=RANGED)], []
    )
    assert "position: within 5 feet of the hero" in banded
    assert "position: beyond 5 feet of the hero" in banded
    legacy = narrator_roster([_enemy("Goblin", hp=9, init=1)], [])
    assert "position:" not in legacy
    assert 'name "Goblin": active, can act\n' in legacy
