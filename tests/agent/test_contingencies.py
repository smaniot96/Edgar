"""EDG-182 contingencies: an action, then an "if" branch the engine evaluates on recorded state.

Conditions: the foe the turn's attack names goes down (`target_down`), or the hero's hit points fall
below (or to, or under) a number. The branch the engine runs is a move (PHB p.192: a move takes no
action). A second attack is refused with the action-economy ruling (PHB p.189). A branch whose condition
does not hold is not taken, and the round logs that. A clause the engine cannot read is asked about.
"""

import random

import pytest

from agent.contingency import HP_AT_OR_BELOW, HP_BELOW, TARGET_DOWN, holds, split_contingency
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.positioning import MELEE, RANGED
from tests.agent.confirming import confirm_turn
from tests.agent.test_combat import _state
from tests.agent.test_positioning import FIGHTER, _archer, _brute, _entries, _foe, _hero_row
from tools.dice import use_rng


@pytest.mark.parametrize(
    ("text", "main", "condition", "threshold", "branch"),
    [
        ("I attack the goblin; if it drops, attack the other goblin", "I attack the goblin", TARGET_DOWN, None, "attack the other goblin"),
        ("I attack the goblin, and if I fall below 5 HP, withdraw", "I attack the goblin", HP_BELOW, 5, "withdraw"),
        ("I attack the goblin. If the goblin drops, move 10 feet away", "I attack the goblin", TARGET_DOWN, None, "move 10 feet away"),
        ("I attack, and if I fall at or below 3 hit points, I back away", "I attack", HP_AT_OR_BELOW, 3, "I back away"),
        ("I attack the goblin and then if it drops, move 10 feet away", "I attack the goblin", TARGET_DOWN, None, "move 10 feet away"),
    ],
)
def test_an_if_clause_splits_into_the_action_and_its_condition(text, main, condition, threshold, branch) -> None:
    split = split_contingency(text)
    assert split.main == main and split.unreadable_if is None
    assert split.contingency is not None
    assert (split.contingency.condition, split.contingency.threshold, split.contingency.branch) == (
        condition,
        threshold,
        branch,
    )


def test_a_turn_without_an_if_clause_is_unchanged() -> None:
    split = split_contingency("I close in on the goblin and attack it")
    assert split.contingency is None and split.unreadable_if is None
    assert split.main == "I close in on the goblin and attack it"


def test_an_if_the_engine_cannot_read_is_reported_not_dropped() -> None:
    split = split_contingency("I attack the goblin if it looks weak")
    assert split.contingency is None and split.unreadable_if == "if it looks weak"


def test_conditions_are_read_from_recorded_state() -> None:
    down = split_contingency("I attack; if it drops, move 10 feet away").contingency
    assert holds(down, target_standing=False, hero_hp=9)
    assert not holds(down, target_standing=True, hero_hp=9)
    below = split_contingency("I attack; if I fall below 5 HP, withdraw").contingency
    assert holds(below, target_standing=None, hero_hp=4)
    assert not holds(below, target_standing=None, hero_hp=5)  # "below" is strict
    at = split_contingency("I attack; if I fall at or below 5 HP, withdraw").contingency
    assert holds(at, target_standing=None, hero_hp=5)


async def _run(fake, combatants, message, *, character=None, seed=0, target="goblin") -> dict:
    """One round as the parser hands it over: the attack names its foe, so the target is never a guess."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(combatants, message)
    if character is not None:
        state["character"] = character
    state["parsed_input"] = ParsedInput(intent="combat", check="attack", target=target)
    with use_rng(random.Random(seed)):
        out = await combat_round_node(state)
        return await confirm_turn(out, state)


def _log(out: dict) -> str:
    return out["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
async def test_a_branch_whose_condition_holds_is_applied_after_the_action(fake_llm_schema, fake_retrieval) -> None:
    # The goblin is nearly dead: the attack must drop it for the "if" to hold. Seeds are tried until a hit.
    for seed in range(200):
        goblin = _brute("Goblin", init=1, band=MELEE)
        goblin["hp_current"], goblin["ac"] = 1, 1
        out = await _run(
            fake_llm_schema,
            [_hero_row(init=20), goblin, _archer("Archer", init=2, band=RANGED)],
            "I attack the goblin; if it drops, move 10 feet toward the archer",
            seed=seed,
        )
        if _foe(out, "Goblin")["hp_current"] == 0:
            break
    assert _foe(out, "Goblin")["hp_current"] == 0, "no seed in range landed the killing hit"
    assert "holds, so" in _log(out), _log(out)
    assert _foe(out, "Archer")["distance_ft"] == 20, "the branch was not applied after the attack"


@pytest.mark.asyncio
async def test_a_branch_whose_condition_does_not_hold_is_not_taken(fake_llm_schema, fake_retrieval) -> None:
    goblin = _brute("Goblin", init=1, band=MELEE)
    goblin["hp_current"] = 500
    out = await _run(
        fake_llm_schema,
        [_hero_row(init=20), goblin, _archer("Archer", init=2, band=RANGED)],
        "I attack the goblin; if it drops, move 10 feet toward the archer",
        seed=3,
    )
    assert _foe(out, "Goblin")["hp_current"] > 0
    assert "did not hold" in _log(out), _log(out)
    assert "distance_ft" not in _foe(out, "Archer"), "the branch moved the archer although the condition failed"
    assert _entries(out, "attack check"), "the action itself still resolves"


@pytest.mark.asyncio
async def test_an_hp_condition_reads_the_heros_recorded_hit_points(fake_llm_schema, fake_retrieval) -> None:
    hurt = {**FIGHTER, "hp_current": 6, "hp_max": 12}
    out = await _run(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=MELEE), _archer("Archer", init=2, band=RANGED)],
        "I dodge; if I fall below 10 HP, move 20 feet toward the archer",
        character=hurt,
    )
    assert _foe(out, "Archer")["distance_ft"] == 10
    unmet = await _run(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=MELEE), _archer("Archer", init=2, band=RANGED)],
        "I dodge; if I fall below 3 HP, move 20 feet toward the archer",
        character=hurt,
    )
    assert "distance_ft" not in _foe(unmet, "Archer")


@pytest.mark.asyncio
async def test_a_second_attack_branch_is_refused_with_the_action_ruling(fake_llm_schema, fake_retrieval) -> None:
    out = await _run(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=MELEE), _brute("Wolf", init=2, band=MELEE)],
        "I attack the goblin; if it drops, attack the wolf",
    )
    assert "A second attack needs its own action" in out["narration"]
    assert out["adjudication_result"].dice_log == [] and out["combat_state"]["round"] == 1


@pytest.mark.asyncio
async def test_a_target_condition_needs_an_attack_this_turn(fake_llm_schema, fake_retrieval) -> None:
    out = await _run(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=MELEE)],
        "I move 10 feet away; if it drops, withdraw",
    )
    assert "needs an attack this turn" in out["narration"]
    assert out["adjudication_result"].dice_log == []


@pytest.mark.asyncio
async def test_a_conditional_turn_is_confirmed_before_any_die(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    text = "I attack the goblin; if it drops, move 10 feet toward the archer"
    state = _state([_hero_row(init=20), _brute("Goblin", init=1, band=MELEE), _archer("Archer", init=2, band=RANGED)], text)
    state["parsed_input"] = ParsedInput(intent="combat", check="attack", target="goblin")
    with use_rng(random.Random(5)):
        out = await combat_round_node(state)
    assert out["narration"].startswith("Confirm before any dice")
    assert "if it drops" in out["narration"]
    assert out["adjudication_result"].dice_log == [] and out["combat_state"]["round"] == 1
    with use_rng(random.Random(5)):
        done = await confirm_turn(out, state)
    assert _entries(done, "attack check")
