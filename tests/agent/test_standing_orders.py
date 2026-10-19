"""EDG-182 standing reaction orders: a Ready that is kept, visible and cancellable.

The order is the hero's Ready (PHB p.194) with the engine's ruling that it is kept after each trigger
until cancelled (PHB Ready is single-use; this is an engine extension, recorded in the contract). Its
trigger is the one the engine observes: a foe closing into melee on its own turn. It fires with the
hero's reaction, once per reaction, and the reaction returns at the start of the hero's turn (PHB p.191).
"""

import json
import random

import pytest

from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.positioning import MELEE, RANGED
from apps.api.services.turn_runner import combat_state_public
from tests.agent.confirming import confirm_turn
from tests.agent.test_combat import _state
from tests.agent.test_positioning import _brute, _entries, _hero_row
from tools.dice import use_rng


def _readied(out: dict) -> list[dict]:
    player = next(c for c in out["combat_state"]["combatants"] if c.get("is_player"))
    return [e for e in player.get("effects") or [] if e["kind"] == "readied"]


async def _round(fake, combat, message, *, seed: int = 0, confirm: bool = True) -> dict:
    """A round on the encounter so far: the reload is a JSON round trip, the parse names no foe.

    `confirm=False` returns the turn as the engine first answers it (the confirmation, if one is asked).
    """
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(None, message)
    if combat is not None:
        state["combat_state"] = json.loads(json.dumps(combat))
    state["parsed_input"] = ParsedInput(intent="combat")
    with use_rng(random.Random(seed)):
        out = await combat_round_node(state)
        return await confirm_turn(out, state) if confirm else out


def _fresh(goblin_band: str = RANGED) -> dict:
    return {
        "id": 7,
        "round": 1,
        "ended": False,
        "combatants": [_hero_row(init=20), _brute("Goblin", init=1, band=goblin_band)],
    }


@pytest.mark.asyncio
async def test_a_standing_order_is_confirmed_then_kept_and_visible(fake_llm_schema, fake_retrieval) -> None:
    out = await _round(
        fake_llm_schema, _fresh(), "I ready an attack whenever the goblin closes in", confirm=False
    )
    assert out["narration"].startswith("Confirm before any dice")
    assert "each time it closes in" in out["narration"]
    assert _entries(out, "attack check") == [] and _readied(out) == []
    done = await _round(fake_llm_schema, out["combat_state"], "yes")
    (order,) = _readied(done)
    assert order["standing"] is True and order["foe_name"] == "Goblin"
    public = combat_state_public(done["combat_state"])
    assert public["standing_orders"] == [{"foe": "Goblin", "summary": "Attack Goblin each time it closes in"}]


@pytest.mark.asyncio
async def test_a_standing_order_fires_on_the_foes_close_in_and_survives_the_lapse(
    fake_llm_schema, fake_retrieval
) -> None:
    fresh = _fresh()
    pending = await _round(
        fake_llm_schema, fresh, "I ready an attack whenever the goblin closes in", confirm=False
    )
    first = await _round(fake_llm_schema, pending["combat_state"], "yes", seed=2)
    # The goblin closed in on its own turn: the readied attack was made with the hero's reaction.
    assert _entries(first, "attack check"), "the standing order did not fire when the goblin closed in"
    assert first["combat_state"]["round"] == 2
    assert _readied(first) and _readied(first)[0]["standing"] is True, "the standing order lapsed"
    hero = next(c for c in first["combat_state"]["combatants"] if c.get("is_player"))
    assert hero.get("reaction_used") is True


@pytest.mark.asyncio
async def test_a_standing_order_fires_again_after_the_reaction_returns(fake_llm_schema, fake_retrieval) -> None:
    pending = await _round(
        fake_llm_schema, _fresh(), "I ready an attack whenever the goblin closes in", confirm=False
    )
    first = await _round(fake_llm_schema, pending["combat_state"], "yes", seed=2)
    # The foe steps back to range (set up here: foes do not retreat on their own in this engine).
    combat = json.loads(json.dumps(first["combat_state"]))
    next(c for c in combat["combatants"] if c["name"] == "Goblin")["band"] = RANGED
    second = await _round(fake_llm_schema, combat, "I dodge", seed=3)
    assert _entries(second, "attack check"), "the standing order did not fire again on the next close-in"
    assert _readied(second)[0]["standing"] is True


@pytest.mark.asyncio
async def test_cancelling_a_standing_order_spends_no_turn_and_rolls_nothing(
    fake_llm_schema, fake_retrieval
) -> None:
    pending = await _round(
        fake_llm_schema, _fresh(), "I ready an attack whenever the goblin closes in", confirm=False
    )
    kept = await _round(fake_llm_schema, pending["combat_state"], "yes", seed=2)
    combat = json.loads(json.dumps(kept["combat_state"]))
    next(c for c in combat["combatants"] if c["name"] == "Goblin")["band"] = RANGED
    out = await _round(fake_llm_schema, combat, "cancel the standing order against the goblin", seed=4)
    assert "Cancelled the standing order against Goblin" in out["narration"]
    assert out["adjudication_result"].dice_log == [] and out["combat_state"]["round"] == 2
    assert _readied(out) == []
    assert "standing_orders" not in combat_state_public(out["combat_state"])


@pytest.mark.asyncio
async def test_cancelling_with_no_order_is_said_plainly(fake_llm_schema, fake_retrieval) -> None:
    out = await _round(fake_llm_schema, _fresh(), "cancel my standing order")
    assert "has no standing order to cancel" in out["narration"]
    assert out["adjudication_result"].dice_log == []


@pytest.mark.asyncio
async def test_a_leaves_reach_trigger_is_refused_not_stored(fake_llm_schema, fake_retrieval) -> None:
    out = await _round(
        fake_llm_schema, _fresh(goblin_band=MELEE), "I ready an attack whenever the goblin leaves reach"
    )
    assert "cannot read that trigger" in out["narration"]
    assert out["adjudication_result"].dice_log == [] and _readied(out) == []


@pytest.mark.asyncio
async def test_cancelling_names_the_order_it_cancels(fake_llm_schema, fake_retrieval) -> None:
    pending = await _round(
        fake_llm_schema, _fresh(), "I ready an attack whenever the goblin closes in", confirm=False
    )
    kept = await _round(fake_llm_schema, pending["combat_state"], "yes", seed=2)
    combat = json.loads(json.dumps(kept["combat_state"]))
    combat["combatants"].append(_brute("Wolf", init=3, band=RANGED))
    out = await _round(fake_llm_schema, combat, "cancel the order against the wolf", seed=4)
    assert "has no standing order against Wolf to cancel" in out["narration"]
    assert _readied(out), "a reply about another foe cancelled the goblin's order"
