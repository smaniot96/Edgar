"""EDG-182 increment 5 (movement): moves in feet, measured against the speed budget (PHB p.192-193).

A foe at range starts 30 feet away and a foe in melee within 5 feet (rulings: encounters record
bands, not distances). A move in feet changes that distance; the band follows from it. The hero's
speed is 30 feet, doubled by Dash. A move over the budget is refused with no roll and no turn spent.
"""

import pytest

from agent.positioning import MELEE, RANGED, feet_refusal, movement_legs, plan_moves
from tests.agent.test_positioning import _brute, _entries, _foe, _hero_row, _turn


def test_move_legs_carry_their_feet() -> None:
    assert movement_legs("I move 10 feet toward the goblin and attack it") == ((MELEE, 2, 10),)
    assert movement_legs("I move 20 feet away from the goblin") == ((RANGED, 2, 20),)
    assert movement_legs("I close in on the goblin") == ((MELEE, 2, None),)


def test_the_budget_is_thirty_feet_or_sixty_with_dash() -> None:
    assert feet_refusal("Eda", 30, dash=False) is None
    assert "can move 30 feet" in feet_refusal("Eda", 40, dash=False)
    assert feet_refusal("Eda", 60, dash=True) is None
    assert "doubled by Dash" in feet_refusal("Eda", 61, dash=True)


def test_closing_stops_at_the_melee_boundary_and_only_crossing_it_changes_band() -> None:
    goblin = {"name": "Goblin", "band": RANGED}
    plan = plan_moves("I move 10 feet toward the goblin", [], [goblin], pick=lambda n: None)
    assert plan.steps == [] and plan.distances[0][1] == 20  # still at range: no band change
    plan = plan_moves("I move 30 feet toward the goblin", [], [goblin], pick=lambda n: None)
    assert plan.steps == [(MELEE, [goblin])] and plan.distances[0][1] == 5


@pytest.mark.asyncio
async def test_a_move_of_thirty_feet_closes_into_melee_then_the_attack_is_rolled(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=RANGED)],
        "I move 30 feet toward the goblin and attack it",
        seed=10,
    )
    assert _foe(out, "Goblin")["band"] == MELEE
    assert _foe(out, "Goblin")["distance_ft"] == 5
    assert _entries(out, "attack check"), "the attack was not rolled after the feet move"


@pytest.mark.asyncio
async def test_a_move_short_of_melee_leaves_the_foe_out_of_reach_and_refuses_the_attack(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=RANGED)],
        "I move 10 feet toward the goblin and attack it",
    )
    assert "cannot reach Goblin" in out["narration"]
    assert out["adjudication_result"].dice_log == [] and out["combat_state"]["round"] == 1
    assert "distance_ft" not in _foe(out, "Goblin"), "a refused move must not change the foe"


@pytest.mark.asyncio
async def test_a_move_over_the_budget_is_refused_before_any_roll(fake_llm_schema, fake_retrieval) -> None:
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=RANGED)],
        "I move 40 feet toward the goblin",
    )
    assert "can move 30 feet" in out["narration"]
    assert out["adjudication_result"].dice_log == [] and out["combat_state"]["round"] == 1
    assert _foe(out, "Goblin")["band"] == RANGED


@pytest.mark.asyncio
async def test_dash_doubles_the_budget_for_a_longer_move(fake_llm_schema, fake_retrieval) -> None:
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=RANGED)],
        "I dash 50 feet toward the goblin",
    )
    assert "can move" not in out["narration"]
    assert out["combat_state"]["economy"]["action"] == "Dash"
    assert _foe(out, "Goblin")["band"] == MELEE


@pytest.mark.asyncio
async def test_leaving_melee_by_feet_provokes_an_opportunity_attack(fake_llm_schema, fake_retrieval) -> None:
    out = await _turn(
        fake_llm_schema,
        [_hero_row(init=20), _brute("Goblin", init=1, band=MELEE)],
        "I move 20 feet away from the goblin",
        seed=4,
    )
    assert _entries(out, "Goblin opportunity attack"), "leaving melee by feet did not provoke"
    # The goblin acts after the hero and closes back to melee on its own turn (the existing rule),
    # so the recorded distance ends at the melee boundary, and the close-in is logged.
    assert "Goblin closes to melee range." in out["adjudication_result"].mechanical_summary
    assert _foe(out, "Goblin")["distance_ft"] == 5
