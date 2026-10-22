"""Desktop playtest regressions: earned position, ordinary actions and safe orientation."""

from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.nodes import input_parser
from agent.nodes.narrator import build_narrator_prompt
from agent.nodes.rules_adjudicator import rules_adjudicator_node
from agent.nodes.story_effects import prepare_story_validation_state, validate_story_effects
from agent.resolution import resolve_from_parsed
from agent.scene_guidance import selected_source_object, source_object_choices
from agent.story_turn import resolved_scene
from agent.supported_rules import actions_for_state, engine_applied_for_state
from apps.api.services.journal import project_player_journal
from apps.api.services.opening_journal import opening_journal_effects
from edgar_core.artifacts import text_digest
from edgar_core.assets import project_player_map
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.story import (
    StoryEffect,
    StoryFact,
    accept_story_effect,
    dm_adjudication_reference,
    story_target_id,
)
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
    assert "place=Unidentified place" in prompt
    assert "Only an accepted transition changes the location" in prompt
    assert state["current_scene_id"] == "quay"


def _arrival_visit(manifest, chunks, state):
    motion = _move(manifest, chunks, state).story_effects[0]
    return motion.model_copy(update={
        "kind": "visit", "source_reference_ids": [_cite(chunks["quay_public"])],
        "player_text": "You follow the crew along the route toward their home.",
    })


def test_linked_arrival_visit_is_normalized_before_validation_and_revalidation(play):
    manifest, chunks, state = play
    visit = _arrival_visit(manifest, chunks, state)
    result = validate_story_effects(AdjudicationResult(success=True, story_effects=[visit]), state)
    [effect] = result.accepted_story_effects
    assert effect.kind == "move" and result.scene_id == "towpath"
    assert effect.player_text == TOWPATH_PUBLIC
    rechecked = validate_story_effects(
        AdjudicationResult(success=True, story_effects=[visit]), state,
    )
    assert rechecked.accepted_story_effects == result.accepted_story_effects
    assert visit.kind == "visit"  # Raw planner receipt remains intact.


@pytest.mark.asyncio
async def test_linked_arrival_visit_preloads_the_same_reviewed_destination(play, monkeypatch):
    manifest, chunks, state = play
    visit = _arrival_visit(manifest, chunks, state)
    state.pop("story_arrival_context")
    calls = []

    def retrieve(manifests, scene_id, flags, **kwargs):
        calls.append(scene_id)
        return {"adventure_context": [chunks["towpath_public"]]}

    monkeypatch.setattr("db.vector.retrieval.retrieve_active_scene", retrieve)
    result = AdjudicationResult(success=True, story_effects=[visit])
    await prepare_story_validation_state(result, state)
    assert calls == ["towpath"]
    assert state["story_arrival_context"] == [chunks["towpath_public"]]
    assert validate_story_effects(result, state).scene_id == "towpath"


@pytest.mark.parametrize("defect", ["unlinked", "gated", "missing_basis", "unknown_source", "stationary"])
def test_arrival_visit_normalization_preserves_movement_guards(play, defect):
    manifest, chunks, state = play
    visit = _arrival_visit(manifest, chunks, state)
    if defect in {"unlinked", "gated"}:
        if defect == "unlinked":
            manifest.scenes[0].links = []
        else:
            manifest.scenes[0].links[0].requires = {"gate_open": "yes"}
        _public_text(manifest, chunks, state, "quay_public", QUAY_PUBLIC)
        visit = visit.model_copy(update={"target": _ref(manifest, "scene", "towpath"), "source_reference_ids": [_cite(chunks["quay_public"])]})
    elif defect == "missing_basis":
        visit = visit.model_copy(update={"achievement_basis": None})
    elif defect == "unknown_source":
        visit = visit.model_copy(update={"source_reference_ids": ["not_a_source"]})
    else:
        _public_text(manifest, chunks, state, "quay_public", "You stand outside the cabin.")
        _public_text(manifest, chunks, state, "towpath_public", "Inside the cabin, books line the walls.")
        state["player_input"] = "I stay outside and call to the crew."
        visit = visit.model_copy(update={"target": _ref(manifest, "scene", "towpath"), "source_reference_ids": [_cite(chunks["quay_public"])]})
    assert validate_story_effects(
        AdjudicationResult(success=True, story_effects=[visit]), state,
    ).accepted_story_effects == []


def test_current_visit_remains_a_visit_and_absent_proposals_stay_absent(play):
    manifest, chunks, state = play
    visit = _arrival_visit(manifest, chunks, state).model_copy(
        update={"target": _ref(manifest, "scene", "quay")},
    )
    result = validate_story_effects(AdjudicationResult(success=True, story_effects=[visit]), state)
    assert [e.kind for e in result.accepted_story_effects] == ["visit"]
    assert validate_story_effects(AdjudicationResult(success=True), state).accepted_story_effects == []


@pytest.mark.parametrize("references,allowed", [
    ("mixed", True), ("history_only", False), ("unknown", False),
    ("cross_manifest", False), ("invalid_knowledge", False),
])
def test_movement_history_origins_are_redundant_only_alongside_current_source_proof(
    play, references, allowed,
):
    manifest, chunks, state = play
    proposal = _arrival_visit(manifest, chunks, state)
    prior_target = _ref(manifest, "scene", "quay")
    origin = dm_adjudication_reference("prior-turn", prior_target, action="I arrive at the quay.", success=True)
    prior = accept_story_effect(
        StoryEffect(kind="move", status="visited", target=prior_target),
        turn_id="prior-turn", source_refs=[origin], evidence_kind="dm_adjudication",
    )
    fact = StoryFact(**prior.model_dump(), fact_id=story_target_id(prior_target))
    state["story_state"] = {"facts": [fact.model_dump()]}
    history_id = origin["reference_id"]
    if references == "cross_manifest":
        state["story_state"]["facts"][0]["source_refs"][0]["manifest_sha256"] = "f" * 64
    ids = [history_id] if references == "history_only" else [_cite(chunks["quay_public"]), history_id]
    if references == "unknown":
        ids.append("unknown_reference")
    proposal = proposal.model_copy(update={
        "source_reference_ids": ids,
        "knowledge_fact_ids": ["unknown_fact" if references == "invalid_knowledge" else fact.fact_id],
    })
    result = validate_story_effects(AdjudicationResult(success=True, story_effects=[proposal]), state)
    assert bool(result.accepted_story_effects) is allowed
    if allowed:
        [effect] = result.accepted_story_effects
        assert effect.kind == "move"
        assert effect.source_reference_ids == [_cite(chunks["quay_public"])]
        assert effect.knowledge_fact_ids == [fact.fact_id]
        assert validate_story_effects(
            AdjudicationResult(success=True, story_effects=[proposal]), state,
        ).accepted_story_effects == result.accepted_story_effects


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


def _public_text(manifest, chunks, state, passage_id, text):
    passage = next(p for p in manifest.passages if p.passage_id == passage_id)
    passage.text_sha256 = text_digest(text)
    passage.end = len(text)
    manifest.reviewed_sha256 = manifest_digest(manifest)
    chunks[passage_id]["text"] = text
    for chunk in chunks.values():
        chunk["manifest_sha256"] = manifest.reviewed_sha256
    state["adventure_manifests"] = {"ai_campaign": manifest.model_dump()}


@pytest.mark.parametrize("action", [
    'I stay in plain view and call, "Come outside." I wait for an answer.',
    'We remain outside and ask the ferryman to answer.',
    'I wait here and call to the people inside.',
])
def test_stationary_dialogue_cannot_accept_an_interior_arrival(play, action):
    manifest, chunks, state = play
    _public_text(manifest, chunks, state, "quay_public", "You stand outside the cabin. Two sailors wave.")
    _public_text(manifest, chunks, state, "towpath_public", "Inside the cabin, books line the walls.")
    state["player_input"] = action
    result = _move(manifest, chunks, state)
    assert result.accepted_story_effects == []
    state["adjudication_result"] = result
    assert resolved_scene(state)["scene_id"] == "quay"
    # The persistence pass must reject the same proposal.
    rechecked = validate_story_effects(
        AdjudicationResult(success=True, story_effects=result.story_effects), state,
    )
    assert rechecked.accepted_story_effects == []


def test_stationary_wait_can_still_advance_an_outdoor_event(play):
    manifest, chunks, state = play
    state["player_input"] = "I stay here and wait for the approaching crew."
    result = _move(manifest, chunks, state)
    assert len(result.accepted_story_effects) == 1


def test_stationary_interior_wait_is_not_mistaken_for_exterior(play):
    manifest, chunks, state = play
    _public_text(manifest, chunks, state, "quay_public", "The cabin kitchen is warm, with a copper kettle steaming on the hearth.")
    _public_text(manifest, chunks, state, "towpath_public", "Inside the cabin kitchen, the crew arrives to speak with you.")
    state["player_input"] = "I stay here and wait for the approaching crew."
    assert len(_move(manifest, chunks, state).accepted_story_effects) == 1


def test_outdoor_approach_cannot_become_interior_stationary_dialogue(play):
    manifest, chunks, state = play
    _public_text(manifest, chunks, state, "quay_public", "Through the treetops, a strange cabin comes into view. Sailors stand on its lawn.")
    _public_text(manifest, chunks, state, "towpath_public", "Inside the cabin, books line the walls.")
    state["player_input"] = 'I stay in plain view and call, "Come outside." I wait for an answer.'
    assert _move(manifest, chunks, state).accepted_story_effects == []


def test_explicit_entry_after_wait_can_accept_an_interior_arrival(play):
    manifest, chunks, state = play
    _public_text(manifest, chunks, state, "towpath_public", "Inside the cabin, books line the walls.")
    state["player_input"] = "I wait here for a reply, then I enter the cabin."
    result = _move(manifest, chunks, state)
    assert len(result.accepted_story_effects) == 1


def test_narrator_treats_actor_staging_as_conditional_not_live_state(play):
    manifest, chunks, state = play
    _public_text(manifest, chunks, state, "towpath_public", "The sheep stays close, holding its scroll.")
    state["player_input"] = "I hand the sheep to the guard and accompany them."
    state["adjudication_result"] = _move(manifest, chunks, state)
    prompt = "\n".join(str(m.content) for m in build_narrator_prompt(state))
    assert "authored opening conditions, not a live snapshot" in prompt
    assert "never restore a departed actor" in prompt
    assert "preserve an explicitly stationary position" in prompt


def test_arrival_staging_is_not_promoted_to_live_current_or_committed_consequences(play):
    manifest, chunks, state = play
    staging = "The animal stays close, holding its scroll."
    _public_text(manifest, chunks, state, "towpath_public", staging)
    state["player_input"] = "I continue openly along the route taken by the guard."
    result = _move(manifest, chunks, state)
    [move] = result.accepted_story_effects
    prior = "The guard takes the animal into his custody and leads it away. You walk beside them."
    state.update(
        adjudication_result=result,
        messages=[AIMessage(content=prior), HumanMessage(content=state["player_input"])],
        story_state={"facts": [StoryFact(**move.model_dump(), fact_id=story_target_id(move.target)).model_dump()]},
    )
    prompt = "\n".join(str(m.content) for m in build_narrator_prompt(state))
    consequences = prompt.split("Validated canonical story consequences this turn", 1)[1].split("Engine outcome", 1)[0]
    assert staging not in consequences
    assert "Place visit only" in consequences
    # The same source prose must not be repeated as a current fact, historical
    # fact, and module excerpt. It remains once in conditional reference context.
    assert prompt.count(staging) == 1
    assert prior in prompt
    assert "use the newest DM narration retained in the budgeted conversation above" in prompt
    assert "A place visit and its authored opening excerpt do not reset these facts" in prompt
    # Projection changes no saved receipt, fact, or public journal description.
    assert move.player_text == staging
    assert state["story_state"]["facts"][0]["player_text"] == staging


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
    assert place.title == journal.current_place.title == "Unidentified place"
    assert place.detail == QUAY_PUBLIC
    assert "The Waterline Falls" not in map_.model_dump_json() + journal.model_dump_json()
    assert QUAY_DM not in str(place)


def test_opening_journal_and_map_preserve_an_explicitly_earned_place_name(play):
    manifest, chunks, _ = play
    text = "You stand outside the Red Lion Inn. The front door is open."
    manifest.scenes[0].title = "Red Lion Inn"
    public = next(p for p in manifest.passages if p.passage_id == "quay_public")
    public.text_sha256 = text_digest(text)
    public.end = len(text)
    manifest.reviewed_sha256 = manifest_digest(manifest)
    chunks["quay_public"].update(text=text, manifest_sha256=manifest.reviewed_sha256)
    [effect] = opening_journal_effects("ai_campaign", manifest, [chunks["quay_public"]], "2014")
    fact = StoryFact(**effect.model_dump(), fact_id=story_target_id(effect.target), event_id=1)
    journal = project_player_journal(
        session_id=2, state={"facts": [fact.model_dump()]}, narration_events=[], current_scene_id="quay",
    )
    map_ = project_player_map(
        session_id=2, collection="ai_campaign", manifest=manifest, facts=[fact],
        world_flags={}, current_scene_id="quay",
    )
    assert map_.places[0].title == journal.current_place.title == "Red Lion Inn"


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
