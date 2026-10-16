"""EDG-044: two-weapon fighting, the off-hand attack as a bonus action.

PHB "Two-Weapon Fighting", PDF p.196: after the Attack action with a light melee weapon, a bonus
action attacks with a different light melee weapon in the other hand, adding no ability modifier to
its damage unless that is negative. Dice are scripted faces, so each assertion is a rolled number.
"""

import pytest

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.resolution import choose_weapon, off_hand_weapon
from agent.supported_rules import EngineApplied, classify_unsupported_actions
from tests.agent.test_class_features import _enemy, _purpose, _sheet, _state
from tests.agent.test_spells import ScriptedDice
from tools.dice import use_rng

RESTRAINED = [{"kind": "restrained", "source": "hands", "applied_round": 1}]  # advantage on it
TWO_BLADES = "I attack the ogre with my shortsword and my off-hand dagger"


def _ogre(*, ac: int = 1, effects: list[dict] | None = None) -> dict:
    # attack_bonus -20: the ogre's one d20 after the hero's turn never hits, so only the hero's
    # dice decide the turn.
    return {**_enemy("Ogre", hp=400, ac=ac, init=1, attack_bonus=-20), "effects": effects or []}


async def _turn(sheet: dict, message: str, faces: list[int], *, foe=None, raging=False) -> dict:
    from agent.graph_combat import combat_round_node

    state = _state(sheet, [foe or _ogre()], message)
    state["parsed_input"] = ParsedInput(intent="combat", check="attack", target="ogre")
    if raging:
        state["combat_state"]["combatants"][0].update(raging=True, rage_round=1)
    with use_rng(dice := ScriptedDice(faces)):
        out = await combat_round_node(state)
    assert not dice.faces, f"the script was not used up: {dice.faces}"
    return out


@pytest.fixture(autouse=True)
def _adjudicator(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)


@pytest.mark.parametrize(
    ("weapons", "hint", "expected"),
    [
        (["shortsword", "dagger"], "", "dagger"),
        (["shortsword", "dagger"], "I stab with my dagger", "shortsword"),
        (["dagger", "dagger"], "", "dagger"),  # two copies of one weapon type are two entries
        (["handaxe", "light hammer"], "", "light hammer"),
        (["dagger"], "", None),  # nothing in the other hand
        (["shortsword", "dagger", "shield"], "", None),  # the shield fills the other hand
        (["longsword", "dagger"], "", None),  # the longsword is not light
        (["rapier", "dagger"], "I stab with my dagger", None),  # neither is the rapier
        (["dagger", "hand crossbow"], "", None),  # light, but not melee
        (["hand crossbow", "hand crossbow"], "", None),
    ],
)
def test_off_hand_weapon_is_a_different_light_melee_weapon_and_no_shield(
    weapons, hint, expected
) -> None:
    sheet = _sheet("Fighter", 2, weapons=weapons)
    off = off_hand_weapon(sheet, choose_weapon(sheet, hint))
    assert (off.name if off else None) == expected


@pytest.mark.asyncio
async def test_off_hand_attack_is_a_bonus_action_without_the_modifier() -> None:
    sheet = _sheet("Fighter", 2, weapons=("shortsword", "dagger"))  # STR 16: +3
    # Main hand: d20 10 hits AC 1, 1d6+3 rolls 4. Off hand: d20 12 hits, 1d4 (no +3) rolls 3.
    # The ogre's d20 2 misses.
    out = await _turn(sheet, TWO_BLADES, [10, 4, 12, 3, 2])
    adj = out["adjudication_result"]
    assert [d["expression"] for d in _purpose(adj.dice_log, "player damage")] == ["1d6+3", "1d4"]
    assert [d["total"] for d in _purpose(adj.dice_log, "player damage")] == [7, 3]
    assert len(_purpose(adj.dice_log, "attack check")) == 2
    assert adj.damage == 10
    assert "hits Ogre with the off-hand dagger for 3 damage" in adj.mechanical_summary
    economy = out["combat_state"]["economy"]
    assert economy["action"] == "attack" and economy["bonus_action"] == "off-hand attack"


@pytest.mark.asyncio
async def test_off_hand_attack_keeps_a_negative_modifier() -> None:
    weak = _sheet("Fighter", 2, weapons=("club", "club"))
    weak["stats"].update(STR=8, DEX=8)  # -1 to hit and damage
    # Both attacks hit AC 1 (d20 10, total 10 + 2 proficiency - 1 = 11); each damage die rolls 3.
    out = await _turn(
        weak, "I attack the ogre with my club and my off-hand club", [10, 3, 10, 3, 2]
    )
    damage = _purpose(out["adjudication_result"].dice_log, "player damage")
    assert [d["expression"] for d in damage] == [
        "1d4-1",
        "1d4-1",
    ]  # PHB: "unless that modifier is negative"


@pytest.mark.asyncio
async def test_off_hand_critical_hit_doubles_its_dice() -> None:
    sheet = _sheet("Fighter", 2, weapons=("shortsword", "dagger"))
    out = await _turn(sheet, TWO_BLADES, [10, 4, 20, 3, 2, 2])  # off hand: natural 20, 2d4 = 3 + 2
    damage = _purpose(out["adjudication_result"].dice_log, "player damage")
    assert [d["expression"] for d in damage] == ["1d6+3", "2d4"]
    assert (
        "Critical hit! Brann hits Ogre with the off-hand dagger for 5 damage"
        in out["adjudication_result"].mechanical_summary
    )


@pytest.mark.asyncio
async def test_rage_bonus_still_applies_to_the_off_hand_attack() -> None:
    # PHB Rage, PDF p.49: +2 damage on a Strength melee weapon attack. It is not an ability modifier.
    barbarian = _sheet("Barbarian", 1, weapons=("handaxe", "handaxe"))
    out = await _turn(
        barbarian, "I attack the ogre with my handaxe and my off-hand handaxe", [10, 4, 10, 3, 2],
        raging=True,
    )  # fmt: skip
    damage = _purpose(out["adjudication_result"].dice_log, "player damage")
    assert [d["expression"] for d in damage] == ["1d6+5", "1d6+2"]  # +3 STR and +2 rage; +2 rage
    assert out["adjudication_result"].mechanical_summary.count("Rage adds 2.") == 2


@pytest.mark.asyncio
async def test_sneak_attack_adds_its_dice_once_per_turn_across_both_attacks() -> None:
    rogue = _sheet("Rogue", 1, weapons=("shortsword", "dagger"))
    restrained = _ogre(effects=RESTRAINED)  # attacks against it have advantage (2 d20 each)
    # Main hand: 10 and 12 keep 12, hit; damage 4, Sneak Attack 5. Off hand: 11 and 9 keep 11, hit,
    # damage 2 and no second Sneak Attack. The restrained ogre's two d20 are 2 and 3.
    out = await _turn(rogue, TWO_BLADES, [10, 12, 4, 5, 11, 9, 2, 2, 3], foe=restrained)
    dice = out["adjudication_result"].dice_log
    assert len(_purpose(dice, "sneak attack")) == 1
    assert [d["total"] for d in _purpose(dice, "player damage")] == [7, 2]


@pytest.mark.asyncio
async def test_sneak_attack_goes_to_the_off_hand_when_the_main_hand_misses() -> None:
    rogue = _sheet("Rogue", 1, weapons=("shortsword", "dagger"))
    restrained = _ogre(ac=15, effects=RESTRAINED)
    # Main hand: 3 and 4 keep 4, total 9 misses AC 15. Off hand: 14 and 10 keep 14, total 19 hits,
    # damage 2, Sneak Attack 5.
    out = await _turn(rogue, TWO_BLADES, [3, 4, 14, 10, 2, 5, 2, 3], foe=restrained)
    dice = out["adjudication_result"].dice_log
    assert [d["total"] for d in _purpose(dice, "sneak attack")] == [5]
    assert [d["total"] for d in _purpose(dice, "player damage")] == [2]
    assert "misses Ogre." in out["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
async def test_off_hand_attack_finds_no_foe_when_the_main_attack_drops_it() -> None:
    sheet = _sheet("Fighter", 2, weapons=("shortsword", "dagger"))
    weak = {**_ogre(), "hp_max": 3, "hp_current": 3}
    out = await _turn(sheet, TWO_BLADES, [10, 4], foe=weak)  # no d20 is rolled for the off hand
    assert (
        "off-hand attack finds no foe left standing"
        in out["adjudication_result"].mechanical_summary
    )
    assert len(_purpose(out["adjudication_result"].dice_log, "attack check")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("character", "message", "fragment"),
    [
        # The bonus action is already spent (PHB, PDF p.190: one bonus action per turn).
        (
            _sheet("Fighter", 2, weapons=("shortsword", "dagger")),
            "I attack the ogre with my off-hand dagger and use second wind",
            "one bonus action per turn",
        ),
        (
            _sheet("Barbarian", 1, weapons=("handaxe", "handaxe")),
            "I rage and attack the ogre with my off-hand handaxe",
            "one bonus action per turn",
        ),
        # No Attack action this turn, or a ranged one.
        (
            _sheet("Fighter", 2, weapons=("shortsword", "dagger")),
            "I dodge and use my off-hand",
            "Attack action with a light melee weapon",
        ),
        (
            _sheet("Fighter", 2, weapons=("hand crossbow", "hand crossbow")),
            "I shoot the ogre and attack with my off-hand",
            "Attack action with a light melee weapon",
        ),
        # No pair of light melee weapons without a shield.
        (
            _sheet("Fighter", 2, weapons=("longsword", "dagger")),
            "I attack the ogre with my longsword and my off-hand dagger",
            "different light melee weapon",
        ),
        (
            _sheet("Fighter", 2, weapons=("shortsword", "dagger", "shield")),
            TWO_BLADES,
            "different light melee weapon",
        ),
    ],
)
async def test_an_impossible_off_hand_attack_is_refused_before_any_roll(
    character, message, fragment
) -> None:
    out = await _turn(character, message, [])  # an empty script fails on any die
    assert fragment in out["narration"]
    assert out["combat_state"]["round"] == 1
    assert not out["adjudication_result"].dice_log


@pytest.mark.parametrize(
    ("text", "effects", "kinds"),
    [
        ("I attack, then use my bonus action for an off-hand attack", ("bonus_action",), []),
        ("I attack and use two-weapon fighting as a bonus action", ("bonus_action",), []),
        ("I attack, then use my bonus action to hide", ("bonus_action",), ["action_economy"]),
        ("I attack and make an off-hand attack", (), ["action_economy"]),
    ],
)
def test_an_executed_off_hand_attack_is_not_an_unsupported_action(text, effects, kinds) -> None:
    got = classify_unsupported_actions(text, applied=EngineApplied(effects=effects))
    assert [a.kind for a in got] == kinds
