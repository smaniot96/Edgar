"""Inspection/negation do not declare mechanics; later actual uses still do."""

import pytest

from agent.models.parsed_input import ParsedInput
from agent.supported_rules import apply_narration_boundary, classify_unsupported_actions

INSPECT_SCROLL = (
    "I crouch so I do not frighten the sheep, offer my open hand for the scroll, "
    "and inspect its writing to identify the spell before deciding whether to use it."
)


@pytest.mark.parametrize(
    "parsed",
    [None, ParsedInput(intent="exploration", entities={"spell": "Speak with Animals"})],
)
def test_scroll_inspection_has_no_cast_or_condition_warning(parsed):
    actions = classify_unsupported_actions(INSPECT_SCROLL, parsed)
    assert actions == ()
    narration = "The wax seal identifies the scroll as Speak with Animals. You have not used it."
    assert apply_narration_boundary(narration, actions) == narration


@pytest.mark.parametrize(
    "text,kinds",
    [
        ("I inspect the scroll to identify the spell, then cast Fireball at the guard.", ["spell"]),
        ("I inspect the scroll and cast Fireball at the guard.", ["spell"]),
        ("I inspect the scroll and frighten the guard.", ["condition"]),
        ("I do not frighten the sheep and cast Fireball at the guard.", ["spell"]),
        ("I do not cast Fireball. I frighten the guard.", ["condition"]),
        ("I try not to frighten the sheep.", []),
        ("I don't cast the spell; I identify its writing.", []),
        ("I ask the mage about Fireball and Hold Person.", []),
        ("I ask the mage to cast Fireball.", ["spell"]),
        ("I frighten the guard with a terrible vision.", ["condition"]),
    ],
)
def test_scope_is_local_to_the_mentioned_action(text, kinds):
    assert [action.kind for action in classify_unsupported_actions(text)] == kinds


def test_real_unsupported_cast_after_inspection_keeps_the_runtime_boundary():
    text = "I identify the spell on the scroll, then cast Fireball at the guard."
    actions = classify_unsupported_actions(text)
    result = apply_narration_boundary(
        "Your Fireball strikes the guard. You stand by the door.", actions
    )
    assert "Your Fireball strikes" not in result
    assert "You stand by the door." in result
    assert "the declared spell has no engine mechanics" in result
