"""Combat subgraph: engine-decided hits, initiative order, HP ownership, targeting, completion."""

import random

import pytest

from agent.graph_combat import (
    EncounterSetup,
    EnemyStat,
    classify_disposition,
    combat_round_node,
    is_flee,
    roll_initiative,
    select_target,
)
from agent.models.adjudication import AdjudicationResult, CharacterUpdate, FlagUpdate
from agent.models.parsed_input import ParsedInput
from agent.resolution import COMPLETION_FLAG, resolve_attack
from tools.dice import use_rng

FIGHTER = {
    "id": 1,
    "name": "Eda",
    "class": "Fighter",
    "level": 1,
    "hp_current": 12,
    "hp_max": 12,
    "stats": {"STR": 16, "DEX": 12, "CON": 14, "INT": 10, "WIS": 12, "CHA": 8},
    "inventory": {"weapons": ["longsword", "shield"], "armor": "chain mail"},
}


def _enemy(name: str, *, hp: int = 9, ac: int = 12, init: int = 5, boss: bool = False, alive: bool = True) -> dict:
    return {
        "name": name,
        "hp_max": hp,
        "hp_current": hp if alive else 0,
        "ac": ac,
        "attack_bonus": 3,
        "damage_dice": "1d6+1",
        "initiative_bonus": 1,
        "init": init,
        "init_bonus": 1,
        "is_boss": boss,
        "is_player": False,
        "alive": alive,
    }


def _player(init: int = 15, ac: int = 18) -> dict:
    return {
        "name": "player",
        "display_name": "Eda",
        "is_player": True,
        "hp_current": 12,
        "hp_max": 12,
        "ac": ac,
        "alive": True,
        "init": init,
        "init_bonus": 1,
        "second_wind_used": False,
    }


def _state(combatants: list[dict] | None, message: str, **extra) -> dict:
    state = {
        "player_input": message,
        "session_id": 1,
        "campaign_id": 1,
        "messages": [],
        "character": dict(FIGHTER),
        "world_flags": {},
        "npcs": [],
        "adventure_collections": [],
    }
    if combatants is not None:
        state["combat_state"] = {"id": 7, "round": 1, "ended": False, "combatants": combatants}
    state.update(extra)
    return state


# --- pure helpers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I flee!", True),
        ("We retreat back down the corridor.", True),
        ("I run away from the ogre", True),
        ("I turn and run.", True),
        ("I try to escape through the window", True),
        ("I surrender.", True),
        ("I run at the goblin and swing my sword", False),
        ("I run toward the door to block it", False),
        ("I escape the grapple", False),
        ("I will not flee from this fight!", False),
        ("The goblin tries to flee but I chase it", False),
        ("I attack the goblin", False),
    ],
)
def test_flee_detection(text, expected) -> None:
    assert is_flee(text) is expected


def test_disposition_classification() -> None:
    assert classify_disposition("friendly") == "ally"
    assert classify_disposition("Loyal companion") == "ally"
    assert classify_disposition("hostile") == "hostile"
    assert classify_disposition("unfriendly, suspicious") == "hostile"
    assert classify_disposition("neutral") == "neutral"
    assert classify_disposition(None) == "neutral"


def test_initiative_rolls_d20_plus_dex_and_sorts() -> None:
    combatants = [
        {"name": "player", "is_player": True},
        {"name": "goblin", "is_player": False, "initiative_bonus": 2},
        {"name": "ogre", "is_player": False, "initiative_bonus": -1},
    ]
    rng = random.Random(11)
    ref = random.Random(11)
    expected_rolls = [ref.randint(1, 20) for _ in range(3)]
    with use_rng(rng):
        ordered = roll_initiative(combatants, FIGHTER)
    by_name = {c["name"]: c for c in ordered}
    assert by_name["player"]["init"] == expected_rolls[0] + 1  # DEX 12 -> +1
    assert by_name["goblin"]["init"] == expected_rolls[1] + 2
    assert by_name["ogre"]["init"] == expected_rolls[2] - 1
    inits = [c["init"] for c in ordered]
    assert inits == sorted(inits, reverse=True)


def test_attack_hits_only_when_meeting_ac() -> None:
    for seed in range(300):
        res = resolve_attack(FIGHTER, 15, rng=random.Random(seed))
        natural = res.natural
        assert res.modifier == 5 and res.against == "AC" and res.dc == 15
        assert res.success is (natural == 20 or (natural != 1 and natural + 5 >= 15))


def test_select_target_prefers_named_then_first_living() -> None:
    gob, wolf = _enemy("Goblin"), _enemy("Wolf")
    dead = _enemy("Bandit", alive=False)
    assert select_target([dead, gob, wolf], ["wolf"]) == (wolf, None)
    assert select_target([dead, gob, wolf], []) == (gob, None)  # never a phantom "enemy"
    target, question = select_target([gob, wolf], ["dragon"])
    assert target is None and "Goblin" in question
    assert select_target([gob], ["dragon"]) == (gob, None)  # only one foe: obvious target


# --- combat_round_node ---------------------------------------------------------------------


async def _round(fake, state: dict) -> dict:
    """Run the combat node as the graph would: with the parser's output already in state."""
    state.setdefault("parsed_input", fake.responses.get(ParsedInput))
    return await combat_round_node(state)


@pytest.mark.asyncio
async def test_player_attack_vs_ac_and_no_double_hp_counting(fake_llm_schema, fake_retrieval) -> None:
    # LLM claims a huge self-heal and success; the engine must ignore both.
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", check="attack", target="goblin")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True, character_update=CharacterUpdate(hp_delta=50)
    )
    for seed in range(40):
        state = _state([_player(init=20, ac=13), _enemy("Goblin", ac=30, init=1)], "I attack the goblin")
        state["character"] = {**FIGHTER, "hp_current": 12}
        with use_rng(random.Random(seed)):
            out = await _round(fake_llm_schema, state)
        adj = out["adjudication_result"]
        goblin = next(c for c in out["combat_state"]["combatants"] if c["name"] == "Goblin")
        # AC 30 can only be hit by a natural 20.
        if adj.natural_roll != 20:
            assert adj.success is False and goblin["hp_current"] == 9
        else:
            assert adj.success is True and goblin["hp_current"] < 9
        assert adj.against == "AC" and adj.dc == 30 and adj.roll_expression == "1d20+5"
        # Only engine-rolled enemy damage (and Second Wind) touches HP.
        enemy_damage = sum(d["total"] for d in adj.dice_log if d.get("purpose") == "Goblin damage")
        second_wind = sum(d["total"] for d in adj.dice_log if d.get("purpose") == "second wind")
        assert adj.character_update.hp_delta <= 0 or second_wind
        if not second_wind:
            assert adj.character_update.hp_delta == -min(12, enemy_damage)


@pytest.mark.asyncio
async def test_initiative_order_drives_narration_and_player_always_acts(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", check="attack", target="ogre")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state(
        [_player(init=10, ac=30), _enemy("Wolf", init=18), _enemy("Ogre", init=2, hp=30)],
        "I swing at the ogre",
    )
    with use_rng(random.Random(4)):
        out = await _round(fake_llm_schema, state)
    summary = out["adjudication_result"].mechanical_summary
    wolf_at = summary.index("Wolf")
    player_at = summary.index("attack: 1d20+5")
    ogre_at = summary.rindex("Ogre")
    assert wolf_at < player_at < ogre_at  # wolf (18) -> player (10) -> ogre (2)
    cs = out["combat_state"]
    assert [c["name"] for c in cs["initiative"]] == ["Wolf", "Eda", "Ogre"]
    assert cs["initiative"][1]["is_player"] is True and cs["initiative"][1]["ac"] == 30
    assert cs["current_turn"] == "Eda" and cs["current_turn_index"] == 1
    assert cs["initiative_order"] == ["Wolf", "player", "Ogre"]
    assert cs["round"] == 2


@pytest.mark.asyncio
async def test_unknown_target_with_several_foes_asks_without_rolling(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", check="attack", target="dragon")
    state = _state([_player(), _enemy("Goblin"), _enemy("Wolf")], "I attack the dragon")
    out = await _round(fake_llm_schema, state)
    assert "Who do you attack?" in out["narration"]
    assert out["combat_state"]["round"] == 1
    assert out["adjudication_result"].dice_log == []
    assert not any(schema is AdjudicationResult for schema, _ in fake_llm_schema.calls)


@pytest.mark.asyncio
async def test_potion_in_combat_requires_inventory(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", uses_item="potion of healing")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True, character_update=CharacterUpdate(hp_delta=10, inventory_remove=["potion of healing"])
    )
    hurt = {**FIGHTER, "hp_current": 4}
    goblin = _enemy("Goblin", init=1)
    state = _state([_player(init=20, ac=99), goblin], "I drink a potion of healing", character=hurt)
    with use_rng(random.Random(1)):
        out = await _round(fake_llm_schema, state)
    cu = out["adjudication_result"].character_update
    assert cu.hp_delta <= 0  # no potion carried: no healing
    assert cu.inventory_remove == []
    assert "does not have it" in out["adjudication_result"].mechanical_summary

    stocked = {**hurt, "inventory": {**FIGHTER["inventory"], "items": ["Potion of Healing"]}}
    state = _state([_player(init=20, ac=99), _enemy("Goblin", init=1)], "I drink a potion of healing", character=stocked)
    with use_rng(random.Random(1)):
        out = await _round(fake_llm_schema, state)
    cu = out["adjudication_result"].character_update
    heal = next(d["total"] for d in out["adjudication_result"].dice_log if d["purpose"].startswith("healing"))
    enemy_damage = sum(d["total"] for d in out["adjudication_result"].dice_log if d["purpose"] == "Goblin damage")
    # Player (init 20) drinks first, then the goblin swings.
    assert cu.hp_delta == max(0, min(12, 4 + heal) - enemy_damage) - 4
    assert cu.inventory_remove == ["Potion of Healing"]


@pytest.mark.asyncio
async def test_completion_not_triggered_by_non_killing_swing(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", check="attack", target="lich")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True, flags_set=[FlagUpdate(key=COMPLETION_FLAG, value="true")]
    )
    state = _state([_player(init=20, ac=99), _enemy("Lich", hp=100, boss=True, init=1)], "I strike the lich")
    with use_rng(random.Random(3)):
        out = await _round(fake_llm_schema, state)
    adj = out["adjudication_result"]
    assert adj.completion_status == "rejected"
    assert not any(f.key == COMPLETION_FLAG for f in adj.flags_set)


@pytest.mark.asyncio
async def test_boss_kill_with_proposal_completes(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", check="attack", target="lich")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True, flags_set=[FlagUpdate(key=COMPLETION_FLAG, value="true")]
    )
    for seed in range(50):
        state = _state([_player(init=20, ac=99), _enemy("Lich", hp=1, boss=True, init=1)], "I strike the lich")
        with use_rng(random.Random(seed)):
            out = await _round(fake_llm_schema, state)
        if out["combat_state"]["outcome"] == "victory":
            assert out["adjudication_result"].completion_status == "accepted"
            return
    pytest.fail("no seed produced a hit")


@pytest.mark.asyncio
async def test_encounter_respects_ally_dispositions(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", check="attack", target="bandit")
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(
        enemies=[EnemyStat(name="Bandit", hp_max=8), EnemyStat(name="Sildar", hp_max=10)]
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state(
        None,
        "I attack the bandit",
        npcs=[{"name": "Sildar", "disposition": "friendly"}, {"name": "Klarg", "disposition": "hostile"}],
    )
    with use_rng(random.Random(2)):
        out = await _round(fake_llm_schema, state)
    names = [c["name"] for c in out["combat_state"]["combatants"] if not c["is_player"]]
    assert names == ["Bandit"]
    builder_prompt = next(m for s, m in fake_llm_schema.calls if s is EncounterSetup)[1].content
    assert "ALLIES" in builder_prompt and "Sildar" in builder_prompt and "Klarg" in builder_prompt
    assert "<player_action>" in builder_prompt


@pytest.mark.asyncio
async def test_no_named_foe_and_no_roster_asks_instead_of_phantom_enemy(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat")
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(enemies=[])
    out = await _round(fake_llm_schema, _state(None, "I attack!"))
    assert "combat_state" not in out
    assert "Who are you fighting" in out["narration"]


@pytest.mark.asyncio
async def test_narrator_error_propagates(fake_llm_schema, fake_retrieval, monkeypatch) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", check="attack", target="goblin")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)

    async def boom(*_a, **_kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(fake_llm_schema, "ainvoke", boom)
    out = await _round(fake_llm_schema, _state([_player(), _enemy("Goblin")], "I attack the goblin"))
    assert out == {"error": "provider down"}
