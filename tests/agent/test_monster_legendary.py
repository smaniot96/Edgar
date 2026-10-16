"""EDG-043 (fourth slice): legendary actions of exact records, spent after the hero's turn.

Monster Manual, PDF p.12 ("Legendary Actions"): a legendary creature takes its legendary actions
outside its turn, one option at a time and only at the end of another creature's turn, regains the
spent ones at the start of its own turn, and cannot use them while incapacitated. Options read as the
book words them (Adult Black Dragon, PDF p.88): "Tail Attack. The dragon makes a tail attack." and
"Wing Attack (Costs 2 Actions)", a Dexterity save for bludgeoning damage plus a prone and a flight
clause. Only those two shapes are executed; every other option (Detect, spells, movement) stays
declared. The fixtures are synthetic: their creature, numbers and prose are written for the test.
Dice are scripted (`ScriptedDice`).
"""

import json

import pytest

from agent.combatants import add_effect
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.monster_stats import (
    AttackSave,
    MonsterStats,
    legendary_plan,
    narrator_note,
    parse_monster_stats,
)
from tests.agent.test_concentration import _entries, _next_round, _summary
from tests.agent.test_monster_riders import WAIT, _exact, _state
from tests.agent.test_spells import ScriptedDice
from tools.dice import use_rng

TAIL = "Tail Attack. The drake makes a tail attack."
WING = (
    "Wing Attack (Costs 2 Actions). The drake beats its wings. Each creature within 10 feet of the "
    "drake must succeed on a DC 15 Dexterity saving throw or take 7 (2d6) bludgeoning damage and be "
    "knocked prone. The drake can then fly up to half its flying speed."
)
DRAKE_TEXT = f"""Ember Drake
Large dragon, unaligned
Armor Class 14 (natural armor)
Hit Points 45 (6d10 + 12)
Speed 30 ft., fly 60 ft.
STR DEX CON INT WIS CHA
15 (+2) 12 (+1) 14 (+2) 6 (-2) 10 (+0) 8 (-1)
Challenge 2 (450 XP)
Actions
Claw. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 5 (1d6 + 2) slashing damage.
Tail. Melee Weapon Attack: +4 to hit, reach 10 ft., one target. Hit: 9 (2d8) bludgeoning damage.
Legendary Actions
The drake can take 3 legendary actions, choosing from the
options below. Only one legendary action option can be used
at a time and only at the end of another creature's turn. The
drake regains spent legendary actions at the start of its turn.
Detect. The drake makes a Wisdom (Perception) check.
{TAIL}
{WING}
"""


def _stats(text: str = DRAKE_TEXT) -> MonsterStats:
    return parse_monster_stats(text, name="Ember Drake")


def _drake(text: str = DRAKE_TEXT, **fields) -> dict:
    """The exact drake in melee reach of the hero (a legendary melee option needs its band)."""
    return {**_exact(text, "Ember Drake", hp=45), "band": "melee", **fields}


async def _round(foe: dict, faces: list[int], hp: int = 12) -> tuple[dict, ScriptedDice]:
    dice = ScriptedDice(faces)
    with use_rng(dice):
        return await combat_round_node(_state("I wait and watch", [foe], WAIT, hp=hp)), dice


# --- parsing: the budget, the cost and the exact options -----------------------------------------


def test_exact_options_are_read_with_their_cost_and_the_budget() -> None:
    stats = _stats()
    tail, wing = stats.legendary
    assert stats.legendary_actions == 3
    assert (tail.name, tail.cost, tail.attack.name, tail.attack.damage_dice) == (
        "Tail Attack",
        1,
        "Tail",
        "2d8",
    )
    assert (wing.name, wing.cost, wing.attack.damage_dice, wing.attack.damage_type) == (
        "Wing Attack",
        2,
        "2d6",
        "bludgeoning",
    )
    assert wing.attack.save == AttackSave("DEX", 15, half_on_success=False)
    assert not wing.attack.ranged  # "within 10 feet" is read as the foe's melee band
    assert [a.name for a in stats.attacks] == ["Claw", "Tail"]  # no normal action is added
    assert MonsterStats.from_dict(json.loads(json.dumps(stats.to_dict()))) == stats


def test_the_other_options_and_what_the_wing_attack_leaves_out_stay_declared() -> None:
    declared = _stats().declared
    assert "Detect: legendary action not executed" in declared
    assert "Wing Attack: knocked prone and flight not executed" in declared
    assert not any(d.startswith(("Tail Attack", "The drake")) for d in declared)


@pytest.mark.parametrize(
    ("old", "new", "option"),
    [
        (TAIL, "Tail Attack. The drake makes a horn attack.", "Tail Attack"),  # no such attack
        (TAIL, "Tail Attack. The drake makes one attack.", "Tail Attack"),  # which attack?
        (TAIL, "Tail Attack. The drake makes a tail attack or moves.", "Tail Attack"),
        ("7 (2d6)", "9 (2d6)", "Wing Attack (Costs 2 Actions)"),  # the average disagrees
        ("be knocked prone", "be blinded", "Wing Attack (Costs 2 Actions)"),
        (
            " The drake can then fly up to half its flying speed.",
            "",
            "Wing Attack (Costs 2 Actions)",
        ),
    ],
)
def test_other_wording_stays_declared_and_is_not_executed(old: str, new: str, option: str) -> None:
    stats = _stats(DRAKE_TEXT.replace(old, new))
    assert f"{option}: legendary action not executed" in stats.declared
    assert option.split(" (")[0] not in [o.name for o in stats.legendary]


def test_without_a_stated_budget_no_option_is_executed() -> None:
    stats = _stats(DRAKE_TEXT.replace("can take 3 legendary actions", "can act at will"))
    assert stats.legendary_actions == 0 and stats.legendary == ()
    assert "Tail Attack: legendary action not executed" in stats.declared


def test_lair_actions_stay_declared_with_the_reason() -> None:
    text = DRAKE_TEXT + (
        "Lair Actions\nOn initiative count 20 (losing initiative ties), the drake takes a lair "
        "action to cause one of the following effects:\n"
    )
    assert (
        "lair actions: not executed (no encounter data states that the creature is in its lair)"
        in (_stats(text).declared)
    )


def test_a_record_without_legendary_data_reads_as_none() -> None:
    raw = {k: v for k, v in _stats().to_dict().items() if not k.startswith("legendary")}
    stats = MonsterStats.from_dict(raw)
    assert stats.legendary_actions == 0 and stats.legendary == ()


def test_the_costliest_affordable_option_comes_first() -> None:
    stats = _stats()
    assert [o.name for o in legendary_plan(stats, 3)] == ["Wing Attack", "Tail Attack"]
    assert [o.name for o in legendary_plan(stats, 2)] == ["Wing Attack", "Tail Attack"]
    assert [o.name for o in legendary_plan(stats, 1)] == ["Tail Attack"]
    assert legendary_plan(stats, 0) == ()


def test_the_narrator_is_told_the_legendary_options() -> None:
    note = narrator_note(_drake())
    assert "3 legendary actions after the hero's turn" in note
    assert "Tail Attack (costs 1), Wing Attack (costs 2)" in note


# --- the round ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_the_hero_the_costliest_affordable_options_are_used_in_order(
    fake_llm_schema, fake_retrieval
) -> None:
    """Dara's DEX save is +1: 8 + 1 = 9 fails DC 15, so the Wing Attack (2 actions) takes 3 + 4 = 7
    bludgeoning damage. One action is left for the Tail Attack: 10 + 4 = 14 meets AC 14 and 1 + 1 = 2
    damage, so 12 - 7 - 2 = 3 HP. The drake acts after the hero, so its own turn (a Claw, natural 1)
    follows, and the spent actions are already regained.
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    out, dice = await _round(_drake(), [8, 3, 4, 10, 1, 1, 1])
    summary = _summary(out)
    assert dice.faces == []
    assert out["combat_state"]["combatants"][0]["hp_current"] == 3
    assert (
        summary.index("Ember Drake uses a legendary action: Wing Attack (costs 2).")
        < summary.index("Dara fails the DEX save against Ember Drake's Wing Attack")
        < summary.index("Ember Drake uses a legendary action: Tail Attack (costs 1).")
        < summary.index("Ember Drake's Tail hits Dara for 2 bludgeoning damage")
        < summary.index("Ember Drake's Claw misses Dara")
    )
    assert _entries(out, "Dara DEX save (Wing Attack)")[0]["rolls"] == [8]
    assert _entries(out, "Ember Drake Wing Attack damage")[0]["rolls"] == [3, 4]
    assert "legendary_spent" not in out["combat_state"]["combatants"][1]


@pytest.mark.asyncio
async def test_spent_actions_are_saved_and_regained_at_the_start_of_the_foes_turn(
    fake_llm_schema, fake_retrieval
) -> None:
    """The drake acts before the hero (init 20), so it holds its three spent actions between turns
    (saved on the row) and regains them at the start of its next turn: round 2 spends them again."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    foe = _drake(init=20)
    first, dice = await _round(foe, [1, 14, 3, 4, 1])
    assert dice.faces == []
    saved = first["combat_state"]["combatants"][0]
    assert (saved["name"], saved["legendary_spent"]) == ("Ember Drake", 3)
    assert "succeeds on the DEX save against Ember Drake's Wing Attack" in _summary(first)

    follow = _next_round(first, "I wait and watch", WAIT, round_num=2)
    dice = ScriptedDice([1, 14, 3, 4, 1])
    with use_rng(dice):
        second = await combat_round_node(follow)
    assert dice.faces == []
    assert second["combat_state"]["combatants"][0]["legendary_spent"] == 3


@pytest.mark.asyncio
async def test_the_budget_is_never_exceeded(fake_llm_schema, fake_retrieval) -> None:
    """With 2 legendary actions the Wing Attack (2) takes the whole budget; cheapest first would
    have made two Tail Attacks instead."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    foe = _drake(DRAKE_TEXT.replace("can take 3 legendary", "can take 2 legendary"), init=20)
    out, dice = await _round(foe, [1, 8, 3, 4])
    assert dice.faces == []
    assert out["combat_state"]["combatants"][0]["legendary_spent"] == 2
    assert _entries(out, "Ember Drake Tail attack") == []
    assert "uses a legendary action: Tail Attack" not in _summary(out)


@pytest.mark.asyncio
async def test_a_fallen_hero_ends_the_legendary_actions(fake_llm_schema, fake_retrieval) -> None:
    """5 HP minus the Wing Attack's 7 leaves 0: the Tail Attack is not made."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    out, dice = await _round(_drake(), [8, 3, 4], hp=5)
    assert dice.faces == []
    assert "uses a legendary action: Tail Attack" not in _summary(out)


@pytest.mark.asyncio
async def test_a_foe_that_cannot_act_uses_no_legendary_action(
    fake_llm_schema, fake_retrieval
) -> None:
    """Paralyzed is incapacitated (PHB, PDF p.292): the drake uses none, and no die is rolled."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    foe = _drake()
    add_effect(foe, "paralyzed", source="Hold Person", round_applied=1)
    out, _ = await _round(foe, [])
    assert "legendary action" not in _summary(out)


@pytest.mark.asyncio
async def test_a_foe_beyond_reach_uses_no_melee_option(fake_llm_schema, fake_retrieval) -> None:
    """At range the drake's melee options cannot be made; it closes in on its own turn (Claw, a 1)."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    out, dice = await _round(_drake(band="ranged"), [1])
    assert dice.faces == []
    assert "legendary action" not in _summary(out)
