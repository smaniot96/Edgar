"""EDG-044c: the Ready action, the hero's reaction, and the PHB grapple and shove contests.

Ready (PHB p.194) takes the hero's action and holds one melee attack for one trigger: a named foe
closing into melee on its own turn. The attack is made with the hero's reaction, after the foe's move
and before its attacks (PHB p.191). The reaction comes back at the start of the hero's next turn, and
an untriggered readied attack lapses there. Dice are engine-rolled and seeded; the tests assert what
the engine recorded and the order of events, never a particular roll.
"""

import json
import random

import pytest

from agent.combatants import (
    add_effect,
    classify_combat_maneuver,
    contest_defender_bonus,
    effects_of,
    stable_id,
)
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import MonsterAttack, MonsterStats, combat_fields
from agent.readied_action import READY_PHRASE, ReadyOrder, closes_in, ready_order
from agent.supported_rules import (
    SUPPORTED_MECHANICS,
    UNSUPPORTED_MECHANICS,
    EngineApplied,
    apply_narration_boundary,
    classify_unsupported_actions,
    contract_payload,
)
from tests.agent.test_combat import _enemy, _player, _state
from tools.dice import use_rng

HERO = "Eda"
# A turn that declares no attack and no move: the hero holds its ground. A refused turn changes nothing,
# so the foes' closing is tested with a declaration the engine accepts.
WAIT = ParsedInput(intent="combat", check="perception")


def _brute(
    name: str, *, init: int, band: str | None = None, stats_extra: dict | None = None
) -> dict:
    """A melee-only exact record (its one listed attack is melee): it closes in from range."""
    stats = MonsterStats(
        name=name,
        hp_max=30,
        ac=12,
        abilities={"STR": 1, "DEX": 0, **(stats_extra or {})},
        attacks=(MonsterAttack("Club", 3, "1d6+1", "bludgeoning"),),
    )
    row = {
        "name": name,
        "is_player": False,
        "disposition": "active",
        "alive": True,
        "init": init,
        "init_bonus": 0,
        "monster_stats": stats.to_dict(),
        **combat_fields(stats),
    }
    if band is not None:
        row["band"] = band
    return row


def _archer(name: str, *, init: int, band: str = "ranged") -> dict:
    """A ranged-only exact record: it keeps its distance and never closes in."""
    stats = MonsterStats(
        name=name,
        hp_max=12,
        ac=12,
        abilities={"DEX": 1},
        attacks=(MonsterAttack("Shortbow", 3, "1d6+1", "piercing", ranged=True),),
    )
    return {
        "name": name,
        "is_player": False,
        "disposition": "active",
        "alive": True,
        "init": init,
        "init_bonus": 1,
        "monster_stats": stats.to_dict(),
        "band": band,
        **combat_fields(stats),
    }


async def _turn(fake, combatants, message, *, parsed=None, seed=0, state_extra=None):
    fake.responses[ParsedInput] = parsed or ParsedInput(
        intent="combat", check="attack", target="goblin"
    )
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(combatants, message)
    state["parsed_input"] = fake.responses[ParsedInput]
    state.update(state_extra or {})
    with use_rng(random.Random(seed)):
        return await combat_round_node(state)


def _foe(out: dict, name: str) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c["name"] == name)


def _player_row(out: dict) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c.get("is_player"))


def _purposes(out: dict) -> list[str]:
    return [d.get("purpose", "") for d in out["adjudication_result"].dice_log]


def _log(out: dict) -> str:
    return out["adjudication_result"].mechanical_summary


READY_GOBLIN = "I ready an attack: when the goblin closes in, I strike it"


# --- the trigger and the refusals (no roll, no turn spent) ---------------------------------


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("I ready an attack", "must name a trigger"),
        ("I ready an attack when the goblin steps on the trapdoor", "cannot read that trigger"),
        (
            "I ready an attack with my bow when the goblin closes in",
            "Readied attacks are melee here",
        ),
    ],
)
def test_ready_without_a_supported_trigger_is_a_question_with_no_roll(text, fragment) -> None:
    goblin = _brute("Goblin", init=5, band="ranged")
    result = ready_order(text, [goblin], hero=HERO)
    assert isinstance(result, str) and fragment in result
    assert "No roll is made and no turn is spent" in result


def test_ready_names_the_foe_and_refuses_an_unnamed_or_ambiguous_one() -> None:
    goblin, goblin2 = (
        _brute("Goblin", init=5, band="ranged"),
        _brute("Goblin 2", init=4, band="ranged"),
    )
    text = "I ready an attack when the goblin closes in"
    order = ready_order(text, [goblin, goblin2], hero=HERO)
    assert isinstance(order, ReadyOrder) and order.foe_name == "Goblin"
    assert order.foe is goblin and order.foe_id == stable_id(goblin)
    named_second = ready_order(
        "I ready an attack when Goblin 2 closes in", [goblin, goblin2], hero=HERO
    )
    assert named_second.foe_name == "Goblin 2"
    ambiguous = ready_order(
        "I ready an attack when Goblin and Goblin 2 close in", [goblin, goblin2], hero=HERO
    )
    assert isinstance(ambiguous, str) and "which of these foes" in ambiguous


def test_a_foe_that_keeps_its_distance_cannot_be_readied_against() -> None:
    archer = _archer("Archer", init=5)
    assert closes_in(archer) is False
    refusal = ready_order("I ready an attack when the archer closes in", [archer], hero=HERO)
    assert isinstance(refusal, str) and "never trigger" in refusal


@pytest.mark.parametrize(
    "text",
    ["I'm ready to attack the goblin", "Ready, attack the goblin now", "I get ready and strike"],
)
def test_being_ready_to_attack_is_not_the_ready_action(text) -> None:
    assert READY_PHRASE.search(text) is None


def test_legacy_rows_without_a_record_close_in_by_default() -> None:
    assert closes_in(_enemy("Raider", hp=5)) is True


@pytest.mark.asyncio
async def test_readying_against_a_foe_already_in_melee_is_refused_before_any_roll(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=99), _brute("Goblin", init=5, band="melee")],
        READY_GOBLIN,
    )
    assert "already within 5 feet" in _log(out)
    assert "attack check" not in _purposes(out)
    assert effects_of(_player_row(out)) == []


@pytest.mark.asyncio
async def test_ready_and_dodge_in_one_turn_are_refused_as_two_actions(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=99), _brute("Goblin", init=5, band="ranged")],
        "I dodge and ready an attack when the goblin closes in",
    )
    assert "one action per turn" in _log(out) or "only one" in _log(out)
    assert effects_of(_player_row(out)) == []


# --- the readied attack fires once, with the reaction, before the foe's own attack ----------


@pytest.mark.asyncio
async def test_readied_attack_fires_when_the_foe_closes_in_before_its_own_attack(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=99), _brute("Goblin", init=5, band="ranged")],
        READY_GOBLIN,
        seed=3,
    )
    player = _player_row(out)
    goblin = _foe(out, "Goblin")
    # The Ready action itself rolls nothing and holds the attack.
    assert goblin["band"] == "melee"  # the foe closed in on its own turn
    assert "Goblin closes in, and Eda makes the readied attack." in _log(out)
    economy = out["combat_state"]["economy"]
    assert economy["action"] == "Ready"
    assert economy["reaction"] == "readied attack"
    assert player["reaction_used"] is True
    assert effects_of(player) == []  # the readied effect is consumed by its trigger
    purposes = _purposes(out)
    assert purposes.count("attack check") == 1
    assert purposes.index("attack check") < purposes.index("Goblin Club attack")


@pytest.mark.asyncio
async def test_a_spent_reaction_blocks_the_readied_attack_until_the_heros_turn(
    fake_llm_schema, fake_retrieval
) -> None:
    """A foe that acts before the hero, with the hero's reaction already spent, does not trigger the
    readied attack. The hero's next turn starts, the reaction comes back, and the attack lapses."""
    hero = _player(init=20, ac=99)
    hero["reaction_used"] = True
    hero["effects"] = []
    goblin = _brute("Goblin", init=25, band="ranged")  # acts before the hero
    add_effect(
        hero,
        "readied",
        source="attack Goblin when it closes in",
        source_id=stable_id(goblin),
        round_applied=1,
    )
    out = await _turn(fake_llm_schema, [hero, goblin], "I hold my ground", seed=2, parsed=WAIT)
    assert "attack check" not in _purposes(out)  # the readied attack did not fire
    assert _foe(out, "Goblin")["band"] == "melee"  # the foe still closed in
    assert "readied attack lapses" in _log(out)
    assert _player_row(out)["reaction_used"] is False  # back at the start of the hero's turn
    assert "readied" not in [e["kind"] for e in effects_of(_player_row(out))]


@pytest.mark.asyncio
async def test_an_untriggered_readied_attack_lapses_at_the_heros_next_turn(
    fake_llm_schema, fake_retrieval
) -> None:
    hero = _player(init=20, ac=99)
    goblin = _brute("Goblin", init=5, band="ranged")
    goblin["disposition"] = "unconscious"  # it cannot close in, so the trigger never occurs
    add_effect(
        hero,
        "readied",
        source="attack Goblin when it closes in",
        source_id=stable_id(goblin),
        round_applied=1,
    )
    out = await _turn(fake_llm_schema, [hero, goblin], "I hold my ground", seed=1, parsed=WAIT)
    assert "readied attack lapses" in _log(out)
    assert "readied" not in [e["kind"] for e in effects_of(_player_row(out))]
    assert "attack check" not in _purposes(out)


@pytest.mark.asyncio
async def test_a_held_readied_attack_survives_a_save_and_reload_and_fires_after_it(
    fake_llm_schema, fake_retrieval
) -> None:
    """Mid-combat save and reload: a held Ready is JSON state on the hero row (the persisted combat
    state), and after the reload the foe's closing still triggers it, with the weapon the words named."""
    hero, goblin = (
        _player(init=20, ac=99),
        _brute("Goblin", init=25, band="ranged"),
    )  # foe acts first
    add_effect(
        hero,
        "readied",
        source="attack Goblin when it closes in",
        source_id=stable_id(goblin),
        round_applied=1,
    )
    hero["effects"][0]["weapon_hint"] = "with my longsword"
    reloaded = json.loads(json.dumps([hero, goblin]))
    assert effects_of(reloaded[0])[0]["source_id"] == stable_id(goblin)
    out = await _turn(fake_llm_schema, reloaded, "I hold my ground", seed=1, parsed=WAIT)
    assert _foe(out, "Goblin")["band"] == "melee"
    assert "readied" not in [e["kind"] for e in effects_of(_player_row(out))]
    assert "attack check" in _purposes(out)
    assert _purposes(out).index("attack check") < _purposes(out).index("Goblin Club attack")


# --- the reaction is the hero's: a grapple or Ready on the hero's own turn does not use it ------


@pytest.mark.asyncio
async def test_the_hero_reaction_is_a_turn_resource_and_comes_back_at_turn_start(
    fake_llm_schema, fake_retrieval
) -> None:
    hero = _player(init=20, ac=99)
    hero["reaction_used"] = True
    out = await _turn(
        fake_llm_schema,
        [hero, _brute("Goblin", init=5, band="ranged")],
        "I hold my ground",
        seed=1,
        parsed=WAIT,
    )
    assert _player_row(out)["reaction_used"] is False


# --- the grapple and shove contests follow the PHB (PDF p.196-197) ------------------------------


def test_the_target_chooses_the_better_of_strength_and_dexterity() -> None:
    strong = _brute("Brute", init=0, stats_extra={})
    strong["monster_stats"]["abilities"] = {"STR": 4, "DEX": 0}
    assert contest_defender_bonus(strong) == 4
    nimble = _brute("Wolf", init=0)
    nimble["monster_stats"]["abilities"] = {"STR": -1, "DEX": 3}
    assert contest_defender_bonus(nimble) == 3
    legacy = _enemy("Raider", hp=5)
    legacy["initiative_bonus"] = 2
    assert contest_defender_bonus(legacy) == 2


def test_hold_words_are_a_grapple_and_release_is_unchanged() -> None:
    for text in ("I grapple the goblin", "I pin the goblin", "I tie him up with rope"):
        assert classify_combat_maneuver(text).kind == "grappled"
    assert classify_combat_maneuver("I let go of the goblin").kind == "release"
    assert classify_combat_maneuver("I shove the goblin prone").kind == "prone"


def test_a_restraint_does_not_make_a_grapple_real_and_a_grapple_does_not_make_a_restraint() -> None:
    assert [
        a.kind
        for a in classify_unsupported_actions(
            "I pin the goblin", applied=EngineApplied(effects=("restrained",))
        )
    ] == ["maneuver"]
    assert (
        classify_unsupported_actions(
            "I pin the goblin", applied=EngineApplied(effects=("grappled",))
        )
        == ()
    )
    narration = "The goblin is restrained by the rope. What now?"
    kept = apply_narration_boundary(narration, (), applied=EngineApplied(effects=("grappled",)))
    assert "restrained" not in kept and "What now?" in kept


# --- the contract declares the new mechanics and the remainder -----------------------------


def test_contract_declares_ready_and_the_remaining_actions_as_unsupported() -> None:
    supported = {m.id: m for m in SUPPORTED_MECHANICS}
    unsupported = {m.id: m for m in UNSUPPORTED_MECHANICS}
    assert "readied_action" in supported
    assert supported["readied_action"].implementation == "agent.readied_action:ready_order"
    assert "action_economy_remainder" in unsupported
    remainder = unsupported["action_economy_remainder"].boundary
    assert "Help" in remainder and "Hide" in remainder
    payload = repr(contract_payload())
    assert "Ready" in payload and "closes in" in payload
