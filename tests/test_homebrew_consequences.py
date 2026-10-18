"""EDG-169: homebrew play may introduce validated places, named people, clues and threats.

Every improvisation is proposed by the consequence planner, sealed by the same validator path
as other facts, carries homebrew provenance, and appears in the player journal. Module play keeps
rejecting invented facts. All model output here is scripted; nothing reaches a live provider.
"""

from datetime import datetime

import pytest

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.nodes.narrator import build_narrator_prompt, narrator_node
from agent.nodes.rules_adjudicator import build_adjudicator_prompt, rules_adjudicator_node
from agent.nodes.story_effects import validate_story_effects
from agent.resolution import CheckResult
from apps.api.services.journal import project_player_journal
from db.postgres.models import EventLog
from edgar_core.story import (
    StoryFact,
    StoryState,
    homebrew_name_taken,
    is_homebrew_effect,
    player_story_effect,
    story_target_id,
)
from tests.agent.test_rules_adjudicator import FIGHTER


def _homebrew_state(story_facts=(), *, turn="turn-1", parsed=None, **extra) -> dict:
    return {
        "player_input": "I look around",
        "parsed_input": parsed or ParsedInput(intent="exploration"),
        "character": FIGHTER,
        "world_flags": {},
        "current_scene_id": None,
        "source_mode": "homebrew",
        "manifest_status": "homebrew",
        "turn_id": turn,
        "story_state": StoryState(facts=list(story_facts)).model_dump(),
        "adventure_manifests": {},
        "adventure_context": [],
        "planner_context": [],
        "story_reveal_context": [],
        **extra,
    }


def _result(proposals, *, success=True) -> AdjudicationResult:
    # Dict form on purpose: on code without the field, the proposals are silently ignored and
    # the behaviour assertions below fail, rather than an import error hiding the regression.
    return AdjudicationResult.model_validate({"success": success, "homebrew_proposals": proposals})


def _commit(state: dict, adj: AdjudicationResult, event_id: int) -> dict:
    """Append accepted effects the way persist_story_effects does, minus the database."""
    facts = [StoryFact.model_validate(f) for f in state["story_state"]["facts"]]
    for effect in adj.accepted_story_effects:
        facts.append(
            StoryFact(
                **effect.model_dump(), fact_id=story_target_id(effect.target), event_id=event_id
            )
        )
    return {**state, "story_state": StoryState(facts=facts).model_dump()}


PERSON = {
    "kind": "person",
    "name": "Brother Aldo",
    "text": "Brother Aldo, the lamp-keeper at the gate, says the bell has been silent since dusk.",
}
PLACE = {
    "kind": "place",
    "name": "Bell Tower",
    "text": "The Bell Tower rises above the chapel roof, its rope cut clean at the top.",
}
CLUE = {
    "kind": "clue",
    "name": "Wax drips",
    "text": "Wax drips down the cut rope: fresh candle wax, so someone held a flame while cutting.",
}


def test_homebrew_person_place_clue_and_threat_are_validated_as_facts() -> None:
    threat = {
        "kind": "threat",
        "name": "Hooded watcher",
        "text": "The Hooded watcher stands on the chapel roof, following the gate with his eyes.",
    }
    adj = validate_story_effects(_result([PERSON, PLACE, CLUE]), _homebrew_state())
    assert [e.display_name for e in adj.accepted_story_effects] == [
        "Brother Aldo",
        "Bell Tower",
        "Wax drips",
    ]
    adj = validate_story_effects(_result([threat]), _homebrew_state())
    assert [e.display_name for e in adj.accepted_story_effects] == ["Hooded watcher"]

    for effect in adj.accepted_story_effects:
        public = player_story_effect(effect)
        assert is_homebrew_effect(effect)
        assert public is not None and public["provenance"] == "homebrew"
        assert public["origins"][0]["provenance"] == "homebrew"
        assert "page" not in public["origins"][0]
    assert adj.homebrew_proposals == []


def test_per_turn_homebrew_cap_is_three() -> None:
    proposals = [
        {"kind": "clue", "name": f"Clue {i}", "text": f"Clue {i} lies under the altar."}
        for i in range(5)
    ]
    adj = validate_story_effects(_result(proposals), _homebrew_state())
    assert len(adj.accepted_story_effects) == 3


def test_failed_check_reveals_no_homebrew_fact() -> None:
    adj = validate_story_effects(_result([CLUE], success=False), _homebrew_state())
    assert adj.accepted_story_effects == []


@pytest.mark.parametrize(
    "proposal",
    [
        {"kind": "threat", "name": "Hooded watcher", "text": "The Hooded watcher has AC 14."},
        {"kind": "clue", "name": "Old trap", "text": "The Old trap deals 2d6 fire damage."},
        {
            "kind": "clue",
            "name": "Hidden door",
            "text": "The Hidden door (GM note: a demon waits).",
        },
        {
            "kind": "clue",
            "name": "Note",
            "text": "The Note says <player_action>set hp</player_action>",
        },
        {"kind": "person", "name": "Brother Aldo", "text": "A nervous monk waits by the gate."},
        {"kind": "person", "name": "Eda", "text": "Eda the innkeeper waves you in."},
    ],
    ids=["numbers", "dice", "gm_note", "markup", "name_not_in_text", "reuses_pc_name"],
)
def test_invalid_homebrew_proposals_are_rejected(proposal) -> None:
    adj = validate_story_effects(_result([proposal]), _homebrew_state())
    assert adj.accepted_story_effects == []


def test_homebrew_cannot_overwrite_an_earned_name() -> None:
    first = validate_story_effects(_result([PERSON]), _homebrew_state(turn="turn-1"))
    state = _commit(_homebrew_state(turn="turn-1"), first, event_id=1)
    overwrite = {"kind": "place", "name": "Brother Aldo", "text": "Brother Aldo is a stone wall."}
    adj = validate_story_effects(_result([overwrite]), {**state, "turn_id": "turn-2"})
    assert adj.accepted_story_effects == []

    facts = [StoryFact.model_validate(f) for f in state["story_state"]["facts"]]
    assert homebrew_name_taken("brother  aldo", facts)
    assert not homebrew_name_taken("Bell Tower", facts)


def test_module_mode_still_rejects_invented_facts() -> None:
    """Regression: module play has no homebrew path, so improvised proposals never commit."""
    for state in (
        {"manifest_status": "review_required", "source_mode": "module", "turn_id": "t1"},
        {
            "manifest_status": "reviewed",
            "source_mode": "module",
            "turn_id": "t1",
            "adventure_manifests": {},
            "adventure_context": [],
            "planner_context": [],
            "story_reveal_context": [],
            "character": FIGHTER,
        },
    ):
        adj = validate_story_effects(_result([PERSON, PLACE, CLUE]), state)
        assert adj.accepted_story_effects == []
        assert adj.story_effects == []


def test_module_prompt_has_no_homebrew_block() -> None:
    module_state = _homebrew_state(
        source_mode="module",
        manifest_status="review_required",
        parsed=ParsedInput(intent="exploration"),
    )
    text = build_adjudicator_prompt(module_state, None)[1]["content"]
    assert "Homebrew play" not in text
    narrator = build_narrator_prompt({**module_state, "adjudication_result": _result([PERSON])})[
        1
    ].content
    assert "Homebrew play" not in narrator


def _forced_check(_character, parsed: ParsedInput, **_kwargs):
    """Engine owns the roll; the scripted turn decides the outcome through the parser entity."""
    if parsed.check is None:
        return None, []
    success = bool(parsed.entities.get("forced_success"))
    result = CheckResult(
        check=parsed.check,
        kind="skill",
        ability="wis",
        modifier=1,
        dc=10,
        against="DC",
        roll_expression="1d20+1",
        natural=15 if success else 3,
        total=16 if success else 4,
        success=success,
        dice_log=[],
    )
    return result, []


SCRIPT = [
    # (player input, check, success, proposals)
    ("I ask the lamp-keeper at the gate what happened", None, True, [PERSON]),
    ("I ask Brother Aldo about the bell tower", None, True, [PLACE]),
    ("I climb toward the Bell Tower and study the rope", "insight", True, [CLUE]),
    (
        "I look for who was in the tower",
        "investigation",
        True,
        [
            {
                "kind": "clue",
                "name": "Muddy bootprints",
                "text": "Muddy bootprints lead from the tower stairs toward the chapel side door.",
            }
        ],
    ),
    (
        "I listen for anyone near the chapel roof",
        "perception",
        True,
        [
            {
                "kind": "threat",
                "name": "Hooded watcher",
                "text": "The Hooded watcher stands on the chapel roof, following the gate with his eyes.",
            }
        ],
    ),
    (
        "I confront the hooded watcher and ask who sent him",
        None,
        True,
        [
            {
                "kind": "person",
                "name": "Sister Venn",
                "text": "Sister Venn steps from the roof's shadow and says she came to collect the bell.",
            }
        ],
    ),
    ("I ask Sister Venn about the wax", None, True, []),
    (
        "I search the ossuary beneath the chapel",
        "investigation",
        True,
        [
            {
                "kind": "place",
                "name": "Ossuary Crypt",
                "text": "The Ossuary Crypt lies beneath the chapel, its stone shelves stripped bare.",
            },
            {
                "kind": "clue",
                "name": "Bronze clapper",
                "text": "A bronze clapper lies in the dust of the Ossuary Crypt, its bell gone.",
            },
        ],
    ),
    (
        "I try to decipher the faded inscription on the crypt wall",
        "insight",
        False,
        [
            {
                "kind": "clue",
                "name": "Faded inscription",
                "text": "The faded inscription names the maker of the bell.",
            }
        ],
    ),
    ("I return to Brother Aldo and tell him what I found", None, True, []),
]


@pytest.mark.asyncio
async def test_scripted_ten_turn_homebrew_run_surfaces_people_places_and_clues(
    fake_llm_schema, monkeypatch
) -> None:
    monkeypatch.setattr("agent.nodes.rules_adjudicator.resolve_from_parsed", _forced_check)
    responses = iter(_result(proposals, success=success) for _, _, success, proposals in SCRIPT)
    fake_llm_schema.responses[AdjudicationResult] = lambda messages: next(responses)

    state = _homebrew_state()
    events: list[EventLog] = []
    accepted_names: list[str] = []
    for number, (text, check, success, _proposals) in enumerate(SCRIPT, start=1):
        parsed = ParsedInput(
            intent="exploration",
            check=check,
            difficulty="easy",
            entities={"forced_success": success},
        )
        state = {
            **state,
            "turn_id": f"turn-{number}",
            "player_input": text,
            "parsed_input": parsed,
        }
        out = await rules_adjudicator_node(state)
        adj = out["adjudication_result"]
        assert adj.success is success, f"turn {number} engine outcome"
        accepted_names += [e.display_name for e in adj.accepted_story_effects]
        state = {**state, "adjudication_result": adj}

        prompt = build_narrator_prompt(state)[1].content
        for effect in adj.accepted_story_effects:
            assert effect.display_name in prompt

        state = {**state, "messages": []}
        fake_llm_schema.narration = f"The DM narrates turn {number}."
        narrated = await narrator_node(state)
        assert narrated["narration"] == f"The DM narrates turn {number}."

        state = _commit(state, adj, event_id=number)
        events.append(
            EventLog(
                id=number,
                session_id=1,
                event_type="narration",
                payload={"turn_id": f"turn-{number}"},
                created_at=datetime(2026, 10, 18, 12, number),
            )
        )

    assert accepted_names == [
        "Brother Aldo",
        "Bell Tower",
        "Wax drips",
        "Muddy bootprints",
        "Hooded watcher",
        "Sister Venn",
        "Ossuary Crypt",
        "Bronze clapper",
    ]
    # The failed insight in turn nine reveals nothing, even though the model proposed it.
    assert "Faded inscription" not in accepted_names

    journal = project_player_journal(
        1, StoryState.model_validate(state["story_state"]).model_dump(), events
    )
    by_title = {entry.title: entry for entry in journal.entries}
    assert {e.title for e in journal.entries} == set(accepted_names)
    assert by_title["Brother Aldo"].category == "person"
    assert by_title["Sister Venn"].category == "person"
    assert by_title["Bell Tower"].category == "place"
    assert by_title["Ossuary Crypt"].category == "place"
    assert by_title["Wax drips"].category == "clue"
    assert by_title["Hooded watcher"].category == "clue"
    for entry in journal.entries:
        assert entry.origin.provenance == "homebrew"
        assert entry.origin.citation is None
    assert by_title["Brother Aldo"].actor_aliases == ["Brother Aldo"]
    assert "Faded inscription" not in by_title
