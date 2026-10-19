"""EDG-182: a mechanic the engine does not execute is refused with its ruling, before any roll.

Help needs an ally, and no combatant here is one (PHB p.193). Cover, difficult terrain, high ground and
flanking are not modelled: the scene records no cover, terrain or elevation, and flanking is not a PHB
rule (PHB p.197, p.193). Each refusal names the nearest supported action and spends no turn.
"""

import pytest

from agent.declarations import unsupported_ruling
from agent.models.parsed_input import ParsedInput
from tests.agent.test_combat import _enemy, _player
from tests.agent.test_declaration_confirmation import _say


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("I take the Help action for my ally", "needs an ally"),
        ("I help the goblin fight", "needs an ally"),
        ("I take cover behind the crate", "Cover is not modelled"),
        ("I duck behind the pillar and attack", "Cover is not modelled"),
        ("I move through the difficult terrain", "Difficult terrain is not modelled"),
        ("I attack from the high ground", "High ground is not a PHB rule"),
        ("I flank the goblin with my ally", "Flanking is not a PHB rule"),
    ],
)
def test_each_unsupported_mechanic_has_its_ruling(text, fragment) -> None:
    ruling = unsupported_ruling(text)
    assert ruling is not None and fragment in ruling
    assert "No roll is made" in ruling


def test_an_ordinary_attack_has_no_ruling() -> None:
    assert unsupported_ruling("I attack the goblin") is None


@pytest.mark.asyncio
async def test_a_ruled_mechanic_is_refused_before_any_roll(fake_llm_schema, fake_retrieval) -> None:
    combat = {"id": 7, "round": 1, "ended": False, "combatants": [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)]}
    out = await _say(fake_llm_schema, combat, "I take the Help action for the goblin", parsed=ParsedInput(intent="combat", check="attack", target="goblin"))
    assert "needs an ally" in out["narration"]
    assert out["adjudication_result"].dice_log == [] and out["combat_state"]["round"] == 1
    assert out["combat_state"]["combatants"][1]["hp_current"] == 30
