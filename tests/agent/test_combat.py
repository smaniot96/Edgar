"""Combat subgraph: engine-decided hits, initiative order, HP ownership, targeting, completion."""

import copy
import logging
import random

import pytest

from agent.graph import app
from agent.graph_combat import (
    MAX_COMBAT_ROUNDS,
    EncounterSetup,
    EnemyStat,
    _monster_context,
    _player_ac,
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
    ("armor", "dex", "expected"),
    [
        ("half plate", 16, 17),
        ("half plate", 8, 14),
        ("breastplate", 16, 16),
        ("breastplate", 8, 13),
        ("plate", 16, 18),
        ("splint", 16, 17),
        ("chain mail", 16, 16),
        ("chain shirt", 16, 15),
        ("scale mail", 16, 16),
        ("studded leather", 16, 15),
        ("leather", 16, 14),
        ("", 16, 13),
    ],
)
@pytest.mark.parametrize("shield", [False, True])
def test_player_armor_class(armor: str, dex: int, expected: int, shield: bool) -> None:
    character = {
        "stats": {"DEX": dex},
        "inventory": {"armor": armor, "weapons": ["shield"] if shield else []},
    }
    assert _player_ac(character) == expected + (2 if shield else 0)


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
    target, question = select_target([gob], ["dragon"])
    assert target is None and "Goblin" in question


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
        # Only engine-rolled enemy damage touches HP. No automatic Second Wind at 0 HP; a blow
        # that reaches 0 is a subdued defeat, which leaves the hero at 1 HP.
        enemy_damage = sum(d["total"] for d in adj.dice_log if d.get("purpose") == "Goblin damage")
        assert not [d for d in adj.dice_log if d.get("purpose") == "second wind"]
        expected_hp = max(1 if enemy_damage >= 12 else 0, 12 - enemy_damage)
        assert adj.character_update.hp_delta == expected_hp - 12


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
async def test_encounter_respects_ally_dispositions(
    fake_llm_schema, fake_retrieval, caplog, monkeypatch
) -> None:
    caplog.set_level(logging.INFO, logger="agent.graph_combat")
    monkeypatch.setattr(
        "agent.graph_combat.resolve_creature",
        lambda name, **kw: {
            "creature_id": "bandit",
            "creature_name": "Bandit",
            "text": "Bandit\nArmor Class 12\nHit Points 11\nComplete synthetic Bandit record",
        },
    )
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="bandit"
    )
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(
        enemies=[EnemyStat(name="Bandit", hp_max=8), EnemyStat(name="Sildar", hp_max=10)]
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state(
        None,
        "I attack the bandit",
        npcs=[
            {"name": "Sildar", "disposition": "friendly"},
            {"name": "Klarg", "disposition": "hostile"},
        ],
    )
    with use_rng(random.Random(2)):
        out = await _round(fake_llm_schema, state)
    names = [c["name"] for c in out["combat_state"]["combatants"] if not c["is_player"]]
    assert names == ["Bandit"]
    assert any(
        record.message == "dropped_ally_from_roster" and record.ally_name == "Sildar"
        for record in caplog.records
    )
    builder_prompt = next(m for s, m in fake_llm_schema.calls if s is EncounterSetup)[1].content
    assert "ALLIES" in builder_prompt and "Sildar" in builder_prompt and "Klarg" in builder_prompt
    assert "<player_action>" in builder_prompt


@pytest.mark.asyncio
async def test_monster_lookup_failure_requires_recovery(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="agent.graph_combat")

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("monster corpus unavailable")

    from db.vector.creatures import CreatureResolutionError

    monkeypatch.setattr("agent.graph_combat.resolve_creature", unavailable)
    with pytest.raises(CreatureResolutionError, match="source unavailable"):
        await _monster_context({}, ["Goblin"])
    assert any(
        record.message == "monster_lookup_failed" and record.monster_name == "Goblin"
        for record in caplog.records
    )


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
    assert out["error"] == "provider down"
    assert out["retrieval_degraded"] is False
    assert "source_status" in out


# --- round counting on every ending --------------------------------------------------------

TOUGH = {**FIGHTER, "hp_current": 500, "hp_max": 500}
IDLE = ParsedInput(intent="exploration")  # a turn that declares no attack


class _MaxDice:
    """Every die shows its top face, so each attack hits and crits and no seed is needed."""

    def randint(self, low: int, high: int) -> int:
        return high


async def _fight(
    fake, combatants: list[dict], character: dict, turns: list[tuple[str, ParsedInput]]
) -> list[dict]:
    """Play `turns` of one encounter in order, each fed the combat state the last one persisted."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    outs: list[dict] = []
    with use_rng(_MaxDice()):
        for message, parsed in turns:
            fake.responses[ParsedInput] = parsed
            state = _state(None if outs else combatants, message, character=character)
            if outs:
                state["combat_state"] = copy.deepcopy(outs[-1]["combat_state"])
                hero = next(c for c in outs[-1]["combat_state"]["combatants"] if c["is_player"])
                state["character"] = {**character, "hp_current": hero["hp_current"]}
            outs.append(await _round(fake, state))
    return outs


def _resolved_rounds(outs: list[dict]) -> list[int]:
    """The round each turn resolved, as its telemetry row records it."""
    return [out["telemetry_events"][0]["payload"]["round"] for out in outs]


async def play_victory(fake) -> list[dict]:
    """Round 1 is spent looking around; round 2 kills the goblin."""
    return await _fight(
        fake,
        [_player(init=20, ac=99), _enemy("Goblin", hp=1, ac=1, init=1)],
        TOUGH,
        [
            ("I wait and watch", IDLE),
            ("I attack the goblin", ParsedInput(intent="combat", check="attack", target="goblin")),
        ],
    )


async def play_defeat(fake) -> list[dict]:
    """On max dice each ogre hit is 13, so the hero is knocked out in round 2."""
    return await _fight(
        fake,
        [_player(init=20, ac=99), _enemy("Ogre", hp=500, ac=99, init=1)],
        {**FIGHTER, "hp_current": 20, "hp_max": 20},
        [("I wait and watch", IDLE), ("I wait and watch", IDLE)],
    )


async def play_flee(fake) -> list[dict]:
    """Round 1 is spent looking around; round 2 is the hero leaving the fight."""
    return await _fight(
        fake,
        [_player(init=20, ac=99), _enemy("Ogre", hp=500, ac=99, init=1)],
        TOUGH,
        [("I wait and watch", IDLE), ("I flee!", IDLE)],
    )


async def play_round_limit(fake) -> list[dict]:
    """The ogre outlasts every round the cap allows, so the cap ends the fight."""
    return await _fight(
        fake,
        [_player(init=20, ac=99), _enemy("Ogre", hp=500, ac=99, init=1)],
        TOUGH,
        [("I attack the ogre", ParsedInput(intent="combat", check="attack", target="ogre"))]
        * MAX_COMBAT_ROUNDS,
    )


@pytest.mark.asyncio
async def test_victory_persists_the_round_it_was_won_in(fake_llm_schema, fake_retrieval) -> None:
    outs = await play_victory(fake_llm_schema)
    states = [out["combat_state"] for out in outs]
    assert [s["round"] for s in states] == [2, 2]
    assert [s["ended"] for s in states] == [False, True] and states[-1]["outcome"] == "victory"
    assert _resolved_rounds(outs) == [1, 2]


@pytest.mark.asyncio
async def test_defeat_persists_the_round_it_was_lost_in(fake_llm_schema, fake_retrieval) -> None:
    outs = await play_defeat(fake_llm_schema)
    states = [out["combat_state"] for out in outs]
    assert [s["round"] for s in states] == [2, 2]
    assert [s["ended"] for s in states] == [False, True] and states[-1]["outcome"] == "defeat"
    assert _resolved_rounds(outs) == [1, 2]


@pytest.mark.asyncio
async def test_flee_persists_the_round_it_was_left_in(fake_llm_schema, fake_retrieval) -> None:
    outs = await play_flee(fake_llm_schema)
    states = [out["combat_state"] for out in outs]
    assert [s["round"] for s in states] == [2, 2]
    assert [s["ended"] for s in states] == [False, True] and states[-1]["outcome"] == "fled"
    assert _resolved_rounds(outs) == [1, 2]


@pytest.mark.asyncio
async def test_round_limit_persists_the_last_round_resolved(
    fake_llm_schema, fake_retrieval
) -> None:
    outs = await play_round_limit(fake_llm_schema)
    states = [out["combat_state"] for out in outs]
    assert [s["round"] for s in states] == [*range(2, MAX_COMBAT_ROUNDS + 1), MAX_COMBAT_ROUNDS]
    assert [s["ended"] for s in states] == [False] * (MAX_COMBAT_ROUNDS - 1) + [True]
    assert states[-1]["outcome"] == "fled"
    assert _resolved_rounds(outs) == list(range(1, MAX_COMBAT_ROUNDS + 1))

def _foe_record(name: str) -> dict:
    return {
        "creature_id": name.lower(),
        "creature_name": name,
        "text": f"{name}\nArmor Class 12\nHit Points 9\nComplete synthetic {name} record",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("I'd rather avoid combat with the guards.", None),
        ("Let's talk about the fight with Klarg.", "Klarg"),
        ("I sneak past Klarg, avoiding a fight.", "Klarg"),
    ],
)
async def test_avoiding_or_discussing_a_fight_seeds_no_initiative(
    fake_llm_schema, fake_retrieval, monkeypatch, text, target
) -> None:
    # A hostile Klarg is in the scene, so a seeded roster would fight him (EDG-126).
    monkeypatch.setattr("agent.graph_combat.resolve_creature", lambda name, **kw: _foe_record(name))
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", target=target)
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(
        enemies=[EnemyStat(name="Klarg", hp_max=9)]
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    out = await app.ainvoke(_state(None, text, npcs=[{"name": "Klarg", "disposition": "hostile"}]))
    assert not out.get("combat_state")
    assert not any(schema is EncounterSetup for schema, _ in fake_llm_schema.calls)


@pytest.mark.asyncio
async def test_builder_finding_no_foe_present_starts_no_combat(
    fake_llm_schema, fake_retrieval, monkeypatch
) -> None:
    monkeypatch.setattr("agent.graph_combat.resolve_creature", lambda name, **kw: _foe_record(name))
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", check="attack", target="goblin")
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(enemies=[])
    out = await _round(fake_llm_schema, _state(None, "I attack the goblin"))
    assert "combat_state" not in out
    assert "Who are you fighting" in out["narration"]


@pytest.mark.asyncio
async def test_unresolvable_target_starts_no_combat(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", check="attack", target="unicorn")
    out = await _round(fake_llm_schema, _state(None, "I attack the unicorn"))
    assert "combat_state" not in out
    assert out["error_code"] == "creature_identity_unresolved"


@pytest.mark.asyncio
async def test_attack_on_a_present_hostile_actor_still_starts_combat(
    fake_llm_schema, fake_retrieval, monkeypatch
) -> None:
    monkeypatch.setattr("agent.graph_combat.resolve_creature", lambda name, **kw: _foe_record(name))
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", check="attack", target="goblin")
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(enemies=[EnemyStat(name="goblin", hp_max=9)])
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    with use_rng(random.Random(2)):
        out = await _round(fake_llm_schema, _state(None, "I attack the goblin"))
    combatants = out["combat_state"]["combatants"]
    assert [c["name"] for c in combatants if not c["is_player"]] == ["goblin"]
    assert all("init" in c for c in combatants)
