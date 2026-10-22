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
    linked_transition_choices,
    match_source_quote,
    observation_excerpt,
    observation_span_catalog,
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
def scene_state(monkeypatch, request):
    spine, bundles = sample()
    first = bundles["chapters"][0]["events"][0]
    first["dm_notes"] = getattr(
        request, "param", ANSWER + " The ferryman explains this freely when asked.\n\n" + UNRELATED
    )
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


@pytest.mark.parametrize(
    ("quote", "accepted"),
    [
        ("“Why is departure delayed?”", True),
        ("“When is departure delayed?”", False),
        ("“why is departure delayed?”", False),
    ],
)
async def test_action_quote_allows_only_typographic_quote_changes(scene_state, quote, accepted):
    state = await prepared(scene_state)
    state["player_input"] = 'I ask the ferryman, "Why is departure delayed?"'
    result = validate_story_effects(
        AdjudicationResult(success=True, source_observations=[proposal(state, action_quote=quote)]),
        state,
    )
    assert result.accepted_observations == ([ANSWER] if accepted else [])
    if not accepted:
        assert result.observation_diagnostics["rejected"] == [
            {"index": 0, "reason": "action_quote_not_exact"}
        ]


@pytest.mark.asyncio
async def test_linked_transition_template_binds_current_sources_without_disclosing_destination(
    scene_state,
):
    state = await prepared(scene_state)
    choices = linked_transition_choices(state)
    assert len(choices) == 1
    choice = choices[0]
    assert choice["proposal"]["target"]["canonical_id"] == "event_leads"
    assert "PRIVATE_FUTURE_ROOM" not in str(choice)
    assert choice["proposal"]["source_reference_ids"]
    # A template alone earns no movement. The live DM must judge this actual trigger.
    bare = validate_story_effects(
        AdjudicationResult(success=True, story_effects=[choice["proposal"]]), state
    )
    assert bare.accepted_story_effects == []
    move = {
        **choice["proposal"],
        "achievement_basis": "The player asks the ferryman about the delay, matching this link.",
    }
    result = validate_story_effects(
        AdjudicationResult(success=True, story_effects=[move]), state
    )
    assert result.scene_id == "event_leads"
    assert [effect.kind for effect in result.accepted_story_effects] == ["move"]
    assert not any(effect.kind == "objective" for effect in result.accepted_story_effects)


@pytest.mark.asyncio
async def test_failed_inference_preserves_only_independent_public_observation(scene_state):
    state = await prepared(scene_state)
    state["player_input"] = "I inspect the bell's magic and describe what is plainly visible."
    public_span = next(
        span for span in observation_span_catalog(state).values()
        if "bell hangs motionless" in span["text"]
    )
    private_span = next(
        span for span in observation_span_catalog(state).values() if ANSWER in span["text"]
    )
    common = {
        "action_quote": "describe what is plainly visible",
        "achievement_basis": "This is independent of the failed magical inference.",
        "requires_check_success": False,
    }
    result = AdjudicationResult(
        success=False,
        check="arcana",
        source_observations=[
            {**common, "source_span_id": public_span["source_span_id"],
             "text": public_span["text"].strip()},
            {**common, "source_span_id": private_span["source_span_id"], "text": ANSWER},
        ],
    )
    validate_story_effects(result, state)
    assert result.accepted_observations == [public_span["text"].strip()]
    assert result.observation_diagnostics["rejected"] == [
        {"index": 1, "reason": "failed_check_private_evidence"}
    ]
    assert result.success is False and result.accepted_story_effects == []


@pytest.mark.asyncio
async def test_large_background_evidence_cannot_crowd_out_recent_resolved_continuity(scene_state):
    from langchain_core.messages import AIMessage, HumanMessage

    from edgar_core.model_profiles import effective_profile
    from edgar_core.token_budget import estimate_tokens

    state = await prepared(scene_state)
    answer_span = next(
        span["source_span_id"] for span in observation_span_catalog(state).values()
        if ANSWER in span["text"]
    )
    state["messages"] = [
        HumanMessage(content="I hand the key to the ferryman and follow him."),
        AIMessage(content="The ferryman takes the key and leads you away from the tower."),
    ]
    # Simulate a complete-source registry/PDF binding larger than the prompt budget.
    # Retrieval keeps it, while whole redundant prompt passages can be omitted.
    state["planner_context"].append({
        **state["planner_context"][0],
        "passage_id": "source_page_9999",
        "text": "PRIVATE_BACKGROUND_DUPLICATE " * 10000,
    })
    prompt = build_adjudicator_prompt(state, None)
    rendered = "\n".join(message["content"] for message in prompt)
    assert "The ferryman takes the key and leads you away from the tower." in rendered
    assert ANSWER in rendered
    assert f"[source_span {answer_span}]" in rendered
    assert "PRIVATE_BACKGROUND_DUPLICATE" not in rendered
    assert estimate_tokens(rendered) < effective_profile("play.consequence_planner").max_input_tokens
    assert state["planner_context"][-1]["text"].startswith("PRIVATE_BACKGROUND_DUPLICATE")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scene_state",
    [
        "Mara wants to cross to care for her aunt through winter. Oren urgently needs to "
        "deliver medicine to a sick sibling. Both are willing to board when Hobb confirms "
        "everyone is accounted for and the crossing is safe.\n\n" + UNRELATED
    ],
    indirect=True,
)
async def test_source_backed_npc_replies_reach_narrator_without_executing_offered_plan(scene_state):
    state = await prepared(scene_state)
    state["player_input"] = (
        "I propose a spoken boarding call and ask Mara and Oren whether they agree."
    )
    span = next(
        value
        for value in observation_span_catalog(state).values()
        if "Mara wants to cross" in value["text"]
    )
    reply = (
        "Mara agrees to board if Hobb confirms it is safe; she needs to reach her aunt. "
        "Oren agrees too, explaining that the medicine cannot wait."
    )
    result = AdjudicationResult(
        success=True,
        source_observations=[
            {
                "source_span_id": span["source_span_id"],
                "text": reply,
                "action_quote": "ask Mara and Oren whether they agree",
                "achievement_basis": "Both present travelers answer this proposal according to their stated needs and willingness; the boat has not departed.",
            }
        ],
    )
    validate_story_effects(result, state)
    assert result.accepted_observations == [reply]
    assert result.accepted_story_effects == [] and result.story_effects == []
    assert result.flags_set == [] and result.completion_status is None
    state["adjudication_result"] = result
    planner = build_adjudicator_prompt(state, None)[0]["content"]
    narrator = "\n".join(str(m.content) for m in build_narrator_prompt(state))
    assert "resolve their\n  immediate replies now" in planner
    assert "do not merely say the protagonist asks" in planner
    assert "Do not ask the player what an NPC says" in narrator
    assert reply in narrator and UNRELATED not in narrator


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
    from openai.lib._pydantic import to_strict_json_schema

    schema = convert_to_openai_tool(AdjudicationResult)["function"]["parameters"]
    assert "source_observations" in schema["required"]
    assert "source_observations" in schema["properties"]
    assert "source_span_id" in schema["properties"]["source_observations"]["items"]["required"]
    span_schema = schema["properties"]["source_observations"]["items"]
    strict_span_schema = to_strict_json_schema(AdjudicationResult)["$defs"]["SourceObservation"]
    for model_schema in (span_schema, strict_span_schema):
        assert "source_quote" not in model_schema["properties"]
        assert "source_reference_id" not in model_schema["properties"]
        assert "source_span_id" in model_schema["required"]
    assert "observation_diagnostics" not in schema["properties"]
    assert "accepted_observations" not in schema["properties"]
    validation = AdjudicationResult.model_json_schema(mode="validation")
    assert "source_observations" in validation["required"]
    serialization = AdjudicationResult.model_json_schema(mode="serialization")
    assert "source_observations" not in serialization["properties"]
    assert "source_observations" not in serialization["required"]
    assert AdjudicationResult.model_validate({"success": True}).source_observations == []
    archived = AdjudicationResult.model_validate(
        {
            "success": True,
            "source_observations": [
                {
                    "text": "An ordinary answer.",
                    "source_quote": "The archived literal source.",
                    "source_reference_id": "archived-reference",
                    "action_quote": "I ask",
                    "achievement_basis": "The question earns this ordinary answer.",
                }
            ],
        }
    )
    assert archived.source_observations[0].source_quote == "The archived literal source."
    assert archived.source_observations[0].source_reference_id == "archived-reference"


@pytest.mark.asyncio
async def test_natural_answer_binds_selected_source_span_without_copying_quote(scene_state):
    state = await prepared(scene_state)
    spans = observation_span_catalog(state)
    span_id, span = next((key, value) for key, value in spans.items() if ANSWER in value["text"])
    answer = "The bell signals boarding; it does not make the crossing safe."
    result = AdjudicationResult(
        success=True,
        source_observations=[
            {
                "source_span_id": span_id,
                "text": answer,
                "action_quote": "ask the ferryman",
                "achievement_basis": "The ferryman freely answers the ordinary boarding question.",
            }
        ],
    )
    validate_story_effects(result, state)
    assert result.accepted_observations == [answer]
    assert result.observation_diagnostics["matches"][0]["method"] == "selected_span"
    assert (
        result.observation_diagnostics["matches"][0]["source_reference_id"] == span["reference_id"]
    )
    state["adjudication_result"] = result
    prompt = "\n".join(str(m.content) for m in build_narrator_prompt(state))
    assert answer in prompt and span_id not in prompt and UNRELATED not in prompt
    from apps.api.services.turn_runner import public_adjudication

    assert span_id not in str(public_adjudication(result))
    assert "source_observations" not in result.model_dump()
    # Inline markers replace existing rendering; concatenating exact spans gives
    # the original text, with no copied duplicate source dossier.
    chunk = next(
        c
        for c in active_observation_chunks(state)
        if source_citation(c)["reference_id"] == span["reference_id"]
    )
    rendered = observation_excerpt(chunk, spans)
    assert span_id in rendered
    import re

    assert (
        re.sub(r"\[source_span span_[a-f0-9]+\]\n", "", rendered).split("\n", 1)[1] == chunk["text"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["malformed", "stale", "foreign", "future", "invented_action"])
async def test_selected_span_cannot_escape_exact_current_source_and_action(scene_state, defect):
    state = await prepared(scene_state)
    span_id = next(iter(observation_span_catalog(state)))
    action = "ask the ferryman"
    if defect == "malformed":
        span_id = "span_not-a-real-id"
    elif defect == "stale":
        state["planner_context"] = []
        state["adventure_context"] = []
    elif defect == "foreign":
        span_id = span_id[:-1] + ("a" if span_id[-1] != "a" else "b")
    elif defect == "future":
        state["current_scene_id"] = "event_leads"
    else:
        action = "I search the inaccessible room"
    result = AdjudicationResult(
        success=True,
        source_observations=[
            {
                "source_span_id": span_id,
                "text": ANSWER,
                "action_quote": action,
                "achievement_basis": "A willing ferryman answers the actual question.",
            }
        ],
    )
    validate_story_effects(result, state)
    assert result.accepted_observations == []
    assert result.observation_diagnostics["rejected"]


@pytest.mark.asyncio
async def test_recorded_wrong_reference_relocates_only_exact_current_source_quote(scene_state):
    state = await prepared(scene_state)
    public = state["adventure_context"][0]
    quoted = public["text"]
    value = proposal(state, text="An ordinary visible detail.", source_quote=quoted)
    result = AdjudicationResult(success=True, source_observations=[value])
    validate_story_effects(result, state)
    assert result.accepted_observations == [value["text"]]
    match = result.observation_diagnostics["matches"][0]
    assert match["source_reference_id"] == source_citation(public)["reference_id"]
    assert match["proposed_source_reference_id"] == value["source_reference_id"]


@pytest.mark.asyncio
async def test_private_rejection_receipt_is_separate_from_public_state_and_failure_is_nonfatal(
    scene_state, monkeypatch, tmp_path
):
    import json
    import stat

    from apps.api.services import adventures_meta
    from apps.api.services.campaign_prep import runtime_evidence
    from apps.api.services.turn_runner import public_adjudication

    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path)
    state = await prepared(scene_state)
    state["session_id"] = 1
    state["api_key"] = "NEVER_CAPTURE_CREDENTIALS"
    value = proposal(state, source_quote="PRIVATE_REJECTED_PARAPHRASE")
    result = AdjudicationResult(success=True, source_observations=[value])
    validate_story_effects(result, state)
    paths = list(tmp_path.glob("fresh/preparation-v2/runtime-evidence/*.json"))
    assert len(paths) == 1
    private = json.loads(paths[0].read_text())
    assert private["source_observations"][0]["source_quote"] == value["source_quote"]
    assert stat.S_IMODE(paths[0].stat().st_mode) == 0o600
    assert "NEVER_CAPTURE_CREDENTIALS" not in paths[0].read_text()
    assert value["source_quote"] not in str(result.model_dump())
    assert value["source_quote"] not in str(public_adjudication(result))

    def failed(*_args, **_kwargs):
        raise OSError("private diagnostic disk full")

    monkeypatch.setattr(runtime_evidence, "atomic_json", failed)
    state["turn_id"] = "another-private-diagnostic"
    validate_story_effects(result, state)
    assert result.accepted_observations == []
    assert result.observation_diagnostics["private_capture"] == "unavailable"


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


_VISIBILITY_NOTES = (
    "An Emerald Drake is in the shed.\n\n"
    "Two sailors are on the quay.\n\n"
    "The Emerald Drake is in the shed; it may be detected by sound or smell, "
    "but is not automatically visible."
)


@pytest.mark.asyncio
@pytest.mark.parametrize("scene_state", [_VISIBILITY_NOTES], indirect=True)
@pytest.mark.parametrize("action,check,roll,allowed", [
    ("I continue along the path to the quay.", None, None, False),
    ("I describe the visible surroundings without approaching.", None, None, False),
    ("I inspect the shed for concealed occupants.", "investigation", 18, True),
    ("I listen for sounds from the shed.", None, None, True),
    ("I sniff for smells from the shed.", None, None, True),
])
async def test_explicit_visibility_gate_applies_to_duplicate_private_source_spans(
    scene_state, action, check, roll, allowed,
):
    state = await prepared(scene_state)
    state["player_input"] = action
    spans = observation_span_catalog(state)
    hidden = next(s for s in spans.values() if s["text"].strip() == "An Emerald Drake is in the shed.")
    visible = next(s for s in spans.values() if s["text"].strip() == "Two sailors are on the quay.")
    result = AdjudicationResult(
        success=True, check=check, dice_result=roll,
        source_observations=[
            {
                "source_span_id": s["source_span_id"], "text": s["text"].strip(),
                "action_quote": action, "achievement_basis": "The current action earns this observation.",
            }
            for s in [visible, hidden]
        ],
    )
    validate_story_effects(result, state)
    assert "Two sailors are on the quay." in result.accepted_observations
    assert ("An Emerald Drake is in the shed." in result.accepted_observations) is allowed
    if not allowed:
        assert result.observation_diagnostics["rejected"] == [
            {"index": 1, "reason": "source_visibility_condition_not_met"}
        ]
