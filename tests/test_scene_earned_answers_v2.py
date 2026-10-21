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
    preload_scene_context,
)
from apps.api.services.campaign_prep.compile import assemble, make_extraction
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.sources import source_citation
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
async def test_same_location_event_progress_requires_actual_action_basis_without_goal_completion(
    scene_state,
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
    chunk = state["adventure_context"][0]
    move = {
        "kind": "move",
        "target": candidate["target"],
        "status": "visited",
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
    assert not any(effect.kind == "objective" for effect in result.accepted_story_effects)
