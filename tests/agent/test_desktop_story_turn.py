"""Desktop playtest regressions: earned position, ordinary actions and safe orientation."""

from copy import deepcopy

import pytest

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.nodes import input_parser
from agent.nodes.narrator import build_narrator_prompt
from agent.nodes.rules_adjudicator import rules_adjudicator_node
from agent.nodes.story_effects import validate_story_effects
from agent.resolution import resolve_from_parsed
from agent.scene_guidance import selected_source_object, source_object_choices
from agent.story_turn import resolved_scene
from agent.supported_rules import actions_for_state, engine_applied_for_state
from apps.api.services.journal import project_player_journal
from apps.api.services.opening_journal import opening_journal_effects
from edgar_core.assets import project_player_map
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.story import StoryEffect, StoryFact, story_target_id
from tests.agent.test_journal_recording import (
    QUAY_DM,
    QUAY_PUBLIC,
    TOWPATH_DM,
    TOWPATH_PUBLIC,
    _cite,
    _ref,
    play as _play_fixture,
)
from tests.agent.test_resolution import FIGHTER

play = _play_fixture


def _move(manifest, chunks, state):
    state["parser_scene_context"] = {"event_transitions": [{"scene_id": "towpath"}]}
    state["story_arrival_context"] = [chunks["towpath_public"], chunks["towpath_dm"]]
    return validate_story_effects(
        AdjudicationResult(
            success=True,
            story_effects=[
                StoryEffect(
                    kind="move",
                    status="visited",
                    target=_ref(manifest, "scene", "towpath"),
                    source_reference_ids=[_cite(chunks["quay_dm"])],
                    achievement_basis="The hero follows the crew along the towpath.",
                )
            ],
        ),
        state,
    )


def test_narration_uses_the_same_one_hop_move_that_will_be_committed(play):
    manifest, chunks, state = play
    state["player_input"] = "I follow the crew to their distant house and go inside."
    result = _move(manifest, chunks, state)
    state["adjudication_result"] = result
    # Persistence revalidates exactly these proposals against the unchanged pre-turn state.
    rechecked = validate_story_effects(
        AdjudicationResult(success=True, story_effects=result.story_effects),
        state,
    )
    assert result.accepted_story_effects == rechecked.accepted_story_effects
    assert resolved_scene(state)["scene_id"] == rechecked.scene_id == "towpath"
    prompt = "\n".join(str(m.content) for m in build_narrator_prompt(state))
    assert TOWPATH_PUBLIC in prompt and QUAY_PUBLIC not in prompt
    assert QUAY_DM not in prompt and TOWPATH_DM not in prompt
    assert "place=The towpath is slick with fresh silt" in prompt
    assert "Only an accepted transition changes the location" in prompt
    assert state["current_scene_id"] == "quay"


def test_proposed_or_narrated_position_cannot_move_the_resolved_scene(play):
    _, chunks, state = play
    state["adjudication_result"] = AdjudicationResult(success=True, scene_id="towpath")
    state["story_arrival_context"] = [chunks["towpath_public"]]
    assert resolved_scene(state)["scene_id"] == "quay"
    assert resolved_scene(state)["visible_description"] == [QUAY_PUBLIC]


def test_failed_hidden_search_keeps_the_already_public_surroundings(play):
    _, _, state = play
    state["adjudication_result"] = AdjudicationResult(
        success=False,
        check="investigation",
        dc=15,
        dice_result=4,
    )
    prompt = "\n".join(str(m.content) for m in build_narrator_prompt(state))
    assert QUAY_PUBLIC in prompt
    assert "Already public surroundings, available even when an optional check fails" in prompt
    assert QUAY_DM not in prompt


def test_opening_and_map_share_a_public_label_without_a_private_place_title(play):
    manifest, chunks, _ = play
    # Opening arrival need not disclose the prepared editorial scene title or know-place tag.
    for passage in manifest.passages:
        passage.known_scene_ids = []
    manifest.reviewed_sha256 = manifest_digest(manifest)
    for chunk in chunks.values():
        chunk["manifest_sha256"] = manifest.reviewed_sha256
    effects = opening_journal_effects("ai_campaign", manifest, [chunks["quay_public"]], "2014")
    [effect] = effects
    fact = StoryFact(**effect.model_dump(), fact_id=story_target_id(effect.target), event_id=1)
    journal = project_player_journal(
        session_id=2,
        state={"facts": [fact.model_dump()]},
        narration_events=[],
        current_scene_id="quay",
    )
    map_ = project_player_map(
        session_id=2,
        collection="ai_campaign",
        manifest=manifest,
        facts=[fact],
        world_flags={},
        current_scene_id="quay",
    )
    [place] = map_.places
    assert place.title == journal.current_place.title == "Festival boats bump against the quay"
    assert place.detail == QUAY_PUBLIC
    assert "The Waterline Falls" not in map_.model_dump_json() + journal.model_dump_json()
    assert QUAY_DM not in str(place)


@pytest.fixture
def source_object_state(play):
    manifest, chunks, state = play
    value = manifest.model_dump()
    value["entities"][1]["object_id"] = "animal_scroll"
    value["objects"] = [
        {
            "canonical_id": "animal_scroll",
            "title": "Scroll of Speak with Animals",
            "scene_ids": ["quay"],
            "passage_ids": ["quay_dm"],
            "operations": ["read_aloud"],
            "statuses": ["acquired", "used"],
        }
    ]
    manifest = AdventureManifest.model_validate(value)
    manifest.reviewed_sha256 = manifest_digest(manifest)
    for chunk in chunks.values():
        chunk["manifest_sha256"] = manifest.reviewed_sha256
    chunks["quay_public"]["entity_ids"] = ["orin_dale", "dispatch_slips"]
    state.update(
        adventure_manifests={"ai_campaign": manifest.model_dump()},
        source_mode="module",
        character=deepcopy(FIGHTER),
        player_input="I read the scroll aloud and listen to the animal.",
        parsed_input=ParsedInput(
            intent="exploration",
            source_object_id="animal_scroll",
            entities={"spell": "Speak with Animals"},
            check="spell_attack",
        ),
    )
    return state


@pytest.mark.parametrize(
    "text",
    [
        "I read the scroll aloud and listen to the animal.",
        "I read the Scroll of Speak with Animals aloud.",
        "I read the Speak with Animals scroll aloud.",
        "I use the scroll to speak with animals.",
    ],
)
def test_visible_source_object_corrects_an_inferred_spell_but_not_explicit_casting(
    source_object_state, text
):
    state = source_object_state
    state["player_input"] = text
    assert selected_source_object(state)["object_id"] == "animal_scroll"
    assert actions_for_state(state) == ()
    assert engine_applied_for_state(state).executed_spell is False
    state["player_input"] = "I cast Speak with Animals while holding the scroll."
    assert selected_source_object(state) is None
    assert any(action.kind == "spell" for action in actions_for_state(state))


def test_grounded_source_object_cannot_suppress_an_independent_named_spell(source_object_state):
    state = source_object_state
    state["player_input"] = "I read the scroll, then use Teleport to reach the tower."
    state["parsed_input"] = state["parsed_input"].model_copy(
        update={"entities": {"spell": "Teleport"}}
    )
    assert selected_source_object(state) is None
    assert any(action.kind == "spell" for action in actions_for_state(state))


@pytest.mark.parametrize("defect", ["private_only", "future", "stale", "ambiguous"])
def test_source_object_selection_requires_one_visible_current_reviewed_identity(
    source_object_state, defect
):
    state = source_object_state
    if defect == "private_only":
        state["adventure_context"] = []
    elif defect == "future":
        state["current_scene_id"] = "towpath"
    elif defect == "stale":
        state["adventure_context"][0]["text"] += " tampered"
    elif defect == "ambiguous":
        state["adventure_manifests"]["another"] = state["adventure_manifests"]["ai_campaign"]
        duplicate = {**state["adventure_context"][0], "collection": "another"}
        state["adventure_context"] = [*state["adventure_context"], duplicate]
    assert selected_source_object(state) is None
    if defect != "ambiguous":
        assert source_object_choices(state) == []


@pytest.mark.asyncio
async def test_reading_source_object_reaches_story_planner_without_spell_refusal_or_slot(
    source_object_state, fake_llm_schema
):
    state = source_object_state
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True,
        mechanical_summary="The reading proceeds according to the source procedure.",
    )
    out = await rules_adjudicator_node(state)
    result = out["adjudication_result"]
    assert result.success and "not cast" not in result.mechanical_summary
    assert result.check is None
    assert result.character_update is None or result.character_update.spell_slots_expended == {}
    planner_calls = [call for call in fake_llm_schema.calls if call[0] is AdjudicationResult]
    assert len(planner_calls) == 1
    assert "Grounded source-object interaction" in planner_calls[0][1][1]["content"]


@pytest.mark.parametrize(
    ("text", "check", "reason", "rolls"),
    [
        ("I look around the courtyard.", "perception", None, False),
        ("I search for hidden traps.", "investigation", None, True),
        (
            "I examine the faint tracks.",
            "survival",
            "Faint tracks could be lost among the herd's prints.",
            True,
        ),
        ("I attack the guard.", "attack", None, True),
    ],
)
def test_routine_baseline_needs_no_roll_but_uncertain_or_hostile_actions_do(
    text, check, reason, rolls
):
    parsed = ParsedInput(intent="exploration", check=check, check_reason=reason)
    result, _ = resolve_from_parsed(FIGHTER, parsed, player_input=text)
    assert (result is not None) is rolls


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["yes", "No.", "confirm", "cancel"])
async def test_exact_pending_combat_reply_skips_scene_retrieval_and_parser_provider(
    monkeypatch, text
):
    def forbidden(*args, **kwargs):
        pytest.fail("Exact pending reply must not call scene retrieval or the parser provider")

    monkeypatch.setattr(input_parser, "preload_scene_context", forbidden)
    monkeypatch.setattr(input_parser, "make_chat_model", forbidden)
    state = {
        "player_input": text,
        "character": {"id": 1},
        "combat_state": {
            "combatants": [
                {
                    "is_player": True,
                    "character_id": 1,
                    "pending_declaration": {"player_input": "I move and attack."},
                }
            ]
        },
    }
    out = await input_parser.input_parser_node(state)
    assert out["parsed_input"].intent == "combat"


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["extra_action", "no_pending", "other_hero", "ended"])
async def test_combat_reply_fast_path_cannot_replace_an_action_or_another_heros_declaration(
    fake_llm_schema, defect
):
    pending = {"player_input": "I move and attack."}
    state = {
        "player_input": "yes",
        "character": {"id": 1},
        "combat_state": {
            "combatants": [
                {
                    "is_player": True,
                    "character_id": 1,
                    "pending_declaration": pending,
                }
            ]
        },
    }
    if defect == "extra_action":
        state["player_input"] = "yes, and attack the other guard"
    elif defect == "no_pending":
        state["combat_state"]["combatants"][0].pop("pending_declaration")
    elif defect == "other_hero":
        state["character"]["id"] = 2
    else:
        state["combat_state"]["ended"] = True
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="rp")
    out = await input_parser.input_parser_node(state)
    assert out["parsed_input"].intent == "rp"
    assert len([call for call in fake_llm_schema.calls if call[0] is ParsedInput]) == 1
