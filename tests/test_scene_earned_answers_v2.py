"""The current source can answer an earned question without exposing its private dossier."""

from contextlib import contextmanager
from copy import deepcopy

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from test_campaign_graph_v2 import sample

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.nodes import input_parser, world_retriever
from agent.nodes.narrator import build_narrator_prompt
from agent.nodes.rules_adjudicator import build_adjudicator_prompt
from agent.nodes.story_effects import story_candidates, validate_story_effects
from agent.scene_guidance import (
    active_observation_chunks,
    cached_scene_context,
    match_source_quote,
    preload_scene_context,
)
from apps.api.services.campaign_prep.compile import assemble, make_extraction
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.sources import source_citation
from edgar_core.story import StoryEffect
from ingestion.manifest import manifest_chunks

ANSWER = "The bell is a customary boarding signal, not a safety mechanism."
UNRELATED = "PRIVATE_CULPRIT: Tamsin hid the clapper to delay Mara."


@pytest.fixture
def scene_state(monkeypatch):
    spine, bundles = sample()
    first = bundles["chapters"][0]["events"][0]
    first["dm_notes"] = ANSWER + " The ferryman explains this freely when asked.\n\n" + UNRELATED
    second = deepcopy(first)
    second.update(
        id="event_leads",
        title="Following the ferry lead",
        trigger="The protagonist asks the ferryman about the delay or follows a credible lead.",
        dm_notes="PRIVATE_FUTURE_ROOM contains a hidden trap.",
        next_event_ids=[],
    )
    first["next_event_ids"] = [second["id"]]
    bundles["chapters"][0]["events"].append(second)
    compiled = assemble("fresh", "2014", spine, bundles, [])
    manifest = AdventureManifest.model_validate(compiled["manifest"])
    manifest.reviewed_by = "scene-earned-answer-probe"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    chunks = manifest_chunks(make_extraction(compiled), manifest)
    client = QdrantClient(":memory:")
    client.create_collection("fresh", vectors_config=VectorParams(size=1, distance=Distance.DOT))
    client.upsert(
        "fresh", points=[PointStruct(id=i, vector=[1.0], payload=c) for i, c in enumerate(chunks)]
    )

    @contextmanager
    def active(_client, _collection, query_filter):
        yield query_filter

    monkeypatch.setattr("db.vector.retrieval.active_index_read", active)
    state = {
        "source_mode": "module",
        "manifest_status": "reviewed",
        "turn_id": "earned-answer-probe",
        "player_input": "I ask the ferryman why departure is delayed.",
        "adventure_collections": ["fresh"],
        "adventure_manifests": {"fresh": manifest.model_dump()},
        "current_scene_id": "event_square",
        "rules_edition": "2014",
        "world_flags": {},
        "qdrant_client": client,
        "story_state": {"facts": []},
    }
    yield state
    client.close()


async def prepared(state):
    cache = await preload_scene_context(state)
    state.update(cache["context"])
    state["parser_scene_context"] = cache
    return state


def proposal(state, **changes):
    chunk = next(
        c for c in active_observation_chunks(state) if c["passage_id"] == "event_square_dm"
    )
    value = {
        "text": ANSWER,
        "source_reference_id": source_citation(chunk, "2014")["reference_id"],
        "action_quote": "ask the ferryman",
        "achievement_basis": "The ferryman is present and freely answers this ordinary question about the delay; no hidden motive is being revealed.",
    }
    return {**value, **changes}


@pytest.mark.asyncio
async def test_current_private_answer_reaches_narrator_without_private_dossier(scene_state):
    state = await prepared(scene_state)
    result = AdjudicationResult(success=True, source_observations=[proposal(state)])
    validate_story_effects(result, state)
    assert result.accepted_observations == [ANSWER]
    state["adjudication_result"] = result
    prompt = "\n".join(str(m.content) for m in build_narrator_prompt(state))
    assert ANSWER in prompt
    assert UNRELATED not in prompt
    assert "PRIVATE_FUTURE_ROOM" not in prompt
    assert proposal(state)["achievement_basis"] not in prompt
    serialized = result.model_dump()
    assert "source_observations" not in serialized
    assert serialized["accepted_observations"] == [ANSWER]
    assert result.flags_set == [] and result.character_update is None


@pytest.mark.asyncio
async def test_public_adjudication_hides_raw_source_quotations_and_private_basis(scene_state):
    from apps.api.services.turn_runner import public_adjudication

    state = await prepared(scene_state)
    proposed = proposal(state)
    result = AdjudicationResult(success=True, source_observations=[proposed])
    validate_story_effects(result, state)
    internal = result.model_dump()
    assert internal["accepted_observations"] == [ANSWER]
    # Internal source wording may be a DM-facing statement. Only narrated prose is
    # public; the API must not independently expose that statement as a raw quote.
    public = public_adjudication(result)
    assert "accepted_observations" not in public
    assert "observation_diagnostics" not in public
    assert "source_observations" not in public
    assert ANSWER not in str(public)
    assert proposed["achievement_basis"] not in str(public)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    [
        "failed",
        "fabricated_quote",
        "unknown_reference",
        "invented_action",
        "empty_basis",
        "future_scene",
        "global_registry",
        "changed_source",
    ],
)
async def test_unearned_or_unreviewed_observations_are_rejected(scene_state, defect):
    state = await prepared(scene_state)
    value = proposal(state)
    success = True
    if defect == "failed":
        success = False
    elif defect == "fabricated_quote":
        value["text"] = "The ferry cannot safely sail without a bell."
    elif defect == "unknown_reference":
        value["source_reference_id"] = "unknown"
    elif defect == "invented_action":
        value["action_quote"] = "I open the locker"
    elif defect == "empty_basis":
        value["achievement_basis"] = " "
    elif defect == "future_scene":
        from db.vector.retrieval import retrieve_active_scene

        future = retrieve_active_scene(
            state["adventure_manifests"],
            "event_leads",
            {},
            client=state["qdrant_client"],
            rules_edition="2014",
        )
        chunk = next(c for c in future["planner_context"] if c["passage_id"] == "event_leads_dm")
        state["planner_context"].append(chunk)
        value["source_reference_id"] = source_citation(chunk, "2014")["reference_id"]
        value["text"] = "PRIVATE_FUTURE_ROOM contains a hidden trap."
    elif defect == "global_registry":
        chunk = next(c for c in state["planner_context"] if c["passage_id"] == "campaign_registry")
        value["source_reference_id"] = source_citation(chunk, "2014")["reference_id"]
        value["text"] = chunk["text"][:50]
    elif defect == "changed_source":
        next(c for c in state["planner_context"] if c["passage_id"] == "event_square_dm")[
            "text"
        ] += " unreviewed edit"
    result = AdjudicationResult(
        success=success, source_observations=[value], accepted_observations=["untrusted"]
    )
    validate_story_effects(result, state)
    assert result.accepted_observations == []
    assert result.observation_diagnostics["proposed"] == 1
    assert result.observation_diagnostics["accepted"] == 0
    assert (
        result.observation_diagnostics.get("blocked") or result.observation_diagnostics["rejected"]
    )


def test_model_must_explicitly_decide_source_answers_without_breaking_archived_rulings():
    from langchain_core.utils.function_calling import convert_to_openai_tool

    schema = convert_to_openai_tool(AdjudicationResult)["function"]["parameters"]
    assert "source_observations" in schema["required"]
    assert "source_observations" in schema["properties"]
    assert "source_quote" in schema["properties"]["source_observations"]["items"]["required"]
    assert "observation_diagnostics" not in schema["properties"]
    assert "accepted_observations" not in schema["properties"]
    validation = AdjudicationResult.model_json_schema(mode="validation")
    assert "source_observations" in validation["required"]
    serialization = AdjudicationResult.model_json_schema(mode="serialization")
    assert "source_observations" not in serialization["properties"]
    assert "source_observations" not in serialization["required"]
    assert AdjudicationResult.model_validate({"success": True}).source_observations == []


@pytest.mark.parametrize(
    ("source", "quote", "expected"),
    [
        (
            "Prefix. Hobb can use another clear signal. Tail.",
            "Hobb can use another clear signal.",
            "Hobb can use another clear signal.",
        ),
        (
            "Prefix. Hobb can use\n another\tclear\u00a0signal. Tail.",
            "Hobb can use another clear signal.",
            "Hobb can use\n another\tclear\u00a0signal.",
        ),
        (
            "Prefix. Hobb’s signal—“all aboard”—works… Tail.",
            'Hobb\'s signal-"all aboard"-works...',
            "Hobb’s signal—“all aboard”—works…",
        ),
        (
            "Prefix. Hobb’s signal—“all aboard”—\nworks. Tail.",
            r"Hobb\u2019s signal\u2014\"all aboard\"\u2014\nworks.",
            "Hobb’s signal—“all aboard”—\nworks.",
        ),
    ],
)
def test_source_quote_formatting_recovers_exact_original_span(source, quote, expected):
    result = match_source_quote(source, quote)
    assert result is not None
    assert result["text"] == expected
    assert source[result["start"] : result["end"]] == expected


@pytest.mark.parametrize(
    "quote",
    [
        "Hobb cannot use another clear signal.",
        "Hobb can use a different clear signal.",
        "Another clear signal can be used by Hobb.",
        "Hobb can use another signal.",
        r"Hobb c\u0061n use another clear signal.",
        " ",
    ],
)
def test_source_quote_formatting_never_accepts_paraphrases_or_word_changes(quote):
    assert match_source_quote("Hobb can use another clear signal.", quote) is None


@pytest.mark.asyncio
async def test_natural_answer_is_grounded_by_separate_private_exact_quote(scene_state):
    state = await prepared(scene_state)
    text = "The bell announces boarding; it does not make the crossing safe."
    source_quote = ANSWER + " The ferryman explains this freely when asked."
    result = AdjudicationResult(
        success=True,
        source_observations=[proposal(state, text=text, source_quote=source_quote)],
    )
    validate_story_effects(result, state)
    assert result.accepted_observations == [text]
    assert result.observation_diagnostics["accepted"] == 1
    assert result.observation_diagnostics["matches"][0]["method"] == "exact"
    state["adjudication_result"] = result
    prompt = "\n".join(str(m.content) for m in build_narrator_prompt(state))
    assert text in prompt
    assert source_quote not in prompt
    assert UNRELATED not in prompt
    assert source_quote not in str(result.model_dump())
    # Public source text/offset diagnostics and both private proposal fields remain absent.
    from apps.api.services.turn_runner import public_adjudication

    assert source_quote not in str(public_adjudication(result))
    assert "observation_diagnostics" not in public_adjudication(result)


@pytest.mark.asyncio
async def test_paraphrased_evidence_quote_cannot_ground_even_a_plausible_answer(scene_state):
    state = await prepared(scene_state)
    result = AdjudicationResult(
        success=True,
        source_observations=[proposal(state, source_quote="The bell only announces boarding.")],
    )
    validate_story_effects(result, state)
    assert result.accepted_observations == []
    assert result.observation_diagnostics["rejected"] == [
        {"index": 0, "reason": "source_quote_not_exact"}
    ]


@pytest.mark.asyncio
async def test_parser_receives_current_guidance_and_retriever_reuses_verified_selection(
    scene_state, monkeypatch
):
    from db.vector import retrieval

    original = retrieval.retrieve_active_scene
    reads = []

    def counted(*args, **kwargs):
        reads.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(retrieval, "retrieve_active_scene", counted)
    prompts = []

    class Parser:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, messages):
            prompts.append("\n".join(m.content for m in messages))
            return ParsedInput(intent="rp", check=None)

    monkeypatch.setattr(input_parser, "make_chat_model", lambda **_: Parser())
    monkeypatch.setattr(input_parser, "OPENAI_API_KEY", "test-only")
    out = await input_parser.input_parser_node(scene_state)
    assert out["parsed_input"].check is None
    assert ANSWER in prompts[0]
    assert "ordinary questions" in prompts[0]
    scene_state.update(out)
    assert reads == ["event_square", "event_leads"]
    monkeypatch.setattr(
        world_retriever, "inspect_source", lambda slug, **_: {"slug": slug, "status": "ready"}
    )
    monkeypatch.setattr(world_retriever, "_search_rule_queries", lambda *_, **__: ([], False))
    monkeypatch.setattr(
        world_retriever,
        "retrieve_active_scene",
        lambda *_args, **_kwargs: pytest.fail("repeated scene retrieval"),
    )
    retrieved = await world_retriever._retrieve(scene_state)
    assert retrieved["manifest_status"] == "reviewed"
    assert ANSWER in "\n".join(c["text"] for c in retrieved["planner_context"])
    scene_state["world_flags"] = {"some_new_fact": "true"}
    assert cached_scene_context(scene_state) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["visited", "moved"])
@pytest.mark.parametrize("audience", ["player", "planner"])
async def test_same_location_event_progress_requires_actual_action_basis_without_goal_completion(
    scene_state,
    status,
    audience,
):
    state = await prepared(scene_state)
    prompt = "\n".join(m["content"] for m in build_adjudicator_prompt(state, None))
    assert "Physical relocation is unnecessary" in prompt
    assert "asks the ferryman about the delay" in prompt
    assert "never complete a goal because a scene changed" in prompt
    candidate = next(
        c
        for c in story_candidates(state)
        if c["target"]["kind"] == "scene" and c["target"]["canonical_id"] == "event_leads"
    )
    chunk = (
        state["adventure_context"][0]
        if audience == "player"
        else next(c for c in state["planner_context"] if c["passage_id"] == "event_square_dm")
    )
    move = {
        "kind": "move",
        "target": candidate["target"],
        # The real AI turn proposed moved + a private current event reference.
        "status": status,
        "source_reference_ids": [source_citation(chunk, "2014")["reference_id"]],
    }
    result = validate_story_effects(AdjudicationResult(success=True, story_effects=[move]), state)
    assert result.scene_id == "event_square" and result.accepted_story_effects == []
    move["achievement_basis"] = (
        "The actual action asks the ferryman about the delay, matching the linked event's stated trigger; this advances the conversation in the same place."
    )
    result = validate_story_effects(AdjudicationResult(success=True, story_effects=[move]), state)
    assert result.scene_id == "event_leads"
    assert [effect.kind for effect in result.accepted_story_effects] == ["move"]
    assert result.accepted_story_effects[0].status == "visited"
    if audience == "planner":
        assert result.accepted_story_effects[0].player_text is None
        assert result.accepted_story_effects[0].evidence_kind == "dm_adjudication"
        assert UNRELATED not in str(result.accepted_story_effects[0].model_dump())
    assert not any(effect.kind == "objective" for effect in result.accepted_story_effects)
    # Match persist_turn: reconstruct proposals from the sealed effects, then
    # revalidate against the locked pre-turn state including the verified cache.
    resealed = validate_story_effects(
        AdjudicationResult(
            success=result.success,
            story_effects=[
                StoryEffect.model_validate(effect.model_dump(include=set(StoryEffect.model_fields)))
                for effect in result.accepted_story_effects
            ],
        ),
        state,
    )
    assert resealed.accepted_story_effects == result.accepted_story_effects


@pytest.mark.asyncio
async def test_normalized_move_still_requires_exact_eligible_source(scene_state):
    state = await prepared(scene_state)
    candidate = next(
        c for c in story_candidates(state) if c["target"]["canonical_id"] == "event_leads"
    )
    move = {
        "kind": "move",
        "target": candidate["target"],
        "status": "moved",
        "source_reference_ids": ["invented_reference"],
        "achievement_basis": "The actual question follows the linked trigger.",
    }
    result = validate_story_effects(AdjudicationResult(success=True, story_effects=[move]), state)
    assert result.scene_id == "event_square"
    assert result.accepted_story_effects == []
