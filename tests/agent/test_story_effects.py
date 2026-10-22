"""Disposable reviewed source fixtures: no live provider, DB or source index."""

from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.models.adjudication import AdjudicationResult
from agent.nodes.narrator import build_narrator_prompt
from agent.nodes.rules_adjudicator import build_adjudicator_prompt, rules_adjudicator_node
from agent.nodes.story_effects import (
    prepare_story_validation_state,
    story_candidates,
    validate_story_effects,
)
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest, require_published
from edgar_core.sources import source_citation
from edgar_core.story import StoryEffect, StoryFact, StoryRef, accept_story_effect, story_target_id


def fixture_state(*, reveal_on="success", gated=False):
    text = "The password is copper dawn."
    data = {
        "source": "module.pdf",
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
        "rules_edition": "2014",
        "opening_scene_id": "hall",
        "scenes": [
            {
                "scene_id": "hall",
                "title": "Hall",
                "links": [{"scene_id": "vault", "requires": {"key_found": "discovered"}}],
            },
            {"scene_id": "vault", "title": "Secret future finale"},
        ],
        "entities": [
            {
                "entity_id": "password",
                "kind": "clue",
                "title": "Future betrayal",
                "discovery_flag": "password_found",
                "reveal_on": reveal_on,
            }
        ],
        "passages": [
            {
                "passage_id": "opening",
                "page": 1,
                "start": 0,
                "end": 10,
                "text_sha256": text_digest("A dusty hall."),
                "scene_ids": ["hall"],
                "audience": "player",
            },
            {
                "passage_id": "clue",
                "page": 1,
                "start": 10,
                "end": 50,
                "text_sha256": text_digest(text),
                "scene_ids": ["hall"],
                "entity_ids": ["password"],
                "audience": "player",
                "requires": {"password_found": "discovered"} if gated else {},
            },
            {
                "passage_id": "plan",
                "page": 1,
                "start": 50,
                "end": 90,
                "text_sha256": text_digest("Private future: the duke betrays them."),
                "scene_ids": ["hall"],
                "entity_ids": ["password"],
                "audience": "planner",
            },
        ],
        "objectives": [
            {
                "canonical_id": "learn_password",
                "title": "Learn password",
                "scene_ids": ["hall"],
                "passage_ids": ["clue"],
            }
        ],
        "clocks": [
            {
                "canonical_id": "alarm",
                "title": "Alarm",
                "scene_ids": ["hall"],
                "passage_ids": ["clue"],
                "statuses": ["advanced", "completed"],
                "clock_max": 3,
            }
        ],
        "reviewed_by": "fixture",
    }
    manifest = AdventureManifest.model_validate(data)
    manifest.reviewed_sha256 = manifest_digest(manifest)
    common = {
        "collection": "module",
        "source": "module.pdf",
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
        "manifest_sha256": manifest.reviewed_sha256,
        "rules_edition": "2014",
        "kind": "adventure",
        "page": 1,
        "scene_id": "hall",
    }
    clue = {**common, "passage_id": "clue", "text": text, "audience": "player"}
    private = {
        **common,
        "passage_id": "plan",
        "text": "Private future: the duke betrays them.",
        "audience": "planner",
    }
    state = {
        "manifest_status": "reviewed",
        "adventure_manifests": {"module": manifest.model_dump()},
        "current_scene_id": "hall",
        "world_flags": {},
        "turn_id": "turn-1",
        "story_state": {"facts": []},
        "adventure_context": [] if gated else [clue],
        "planner_context": [private],
        "allowed_scene_ids": ["hall"],
        "rules_edition": "2014",
        "player_input": "I search the writing.",
    }
    return state, clue, private


def proposal(
    state,
    *,
    kind="discover",
    target_kind="entity",
    identity="password",
    status="discovered",
    chunks=None,
    **extra,
):
    manifest = state["adventure_manifests"]["module"]
    return StoryEffect(
        kind=kind,
        target=StoryRef(
            collection="module",
            source_sha256="a" * 64,
            manifest_sha256=manifest["reviewed_sha256"],
            kind=target_kind,
            canonical_id=identity,
        ),
        status=status,
        source_reference_ids=[
            source_citation(c)["reference_id"] for c in chunks or state["adventure_context"]
        ],
        player_text="invented model secret",
        **extra,
    )


def test_exact_quote_provenance_and_private_title_do_not_leak():
    state, clue, private = fixture_state()
    effect = proposal(state, chunks=[clue, private])
    adj = validate_story_effects(AdjudicationResult(success=True, story_effects=[effect]), state)
    accepted = adj.accepted_story_effects[0]
    assert accepted.player_text == clue["text"]
    assert accepted.display_name == "Entity"
    assert len(accepted.source_refs) == 2
    state["adjudication_result"] = adj
    prompt = build_narrator_prompt(state)[-1].content
    assert clue["text"] in prompt
    assert "Private future" not in prompt
    assert "Future betrayal" not in prompt
    assert "invented model secret" not in prompt
    assert "Secret future finale" not in prompt


def test_unknown_sources_private_only_wrong_identity_and_unearned_branch_rejected():
    state, clue, private = fixture_state()
    proposals = [
        proposal(state, chunks=[private]),
        proposal(state).model_copy(update={"source_reference_ids": ["fake"]}),
        proposal(state, kind="move", target_kind="scene", identity="vault", status="visited"),
        proposal(state).model_copy(update={"knowledge_fact_ids": ["unknown"]}),
    ]
    assert not validate_story_effects(
        AdjudicationResult(success=True, story_effects=proposals), state
    ).accepted_story_effects
    assert all(c["target"]["canonical_id"] != "vault" for c in story_candidates(state))


def test_explicit_failure_discovery_and_monotonic_objective_clock():
    state, _, _ = fixture_state(reveal_on="always")
    effect = proposal(state)
    assert validate_story_effects(
        AdjudicationResult(success=False, story_effects=[effect]), state
    ).accepted_story_effects
    objective = proposal(
        state,
        kind="objective",
        target_kind="objective",
        identity="learn_password",
        status="completed",
    )
    accepted = validate_story_effects(
        AdjudicationResult(success=True, story_effects=[objective]), state
    ).accepted_story_effects[0]
    state["story_state"]["facts"] = [
        StoryFact(**accepted.model_dump(), fact_id=story_target_id(accepted.target)).model_dump()
    ]
    regressive = objective.model_copy(update={"status": "active"})
    clock = proposal(
        state, kind="clock", target_kind="clock", identity="alarm", status="advanced", quantity=4
    )
    assert not validate_story_effects(
        AdjudicationResult(success=True, story_effects=[regressive, clock]), state
    ).accepted_story_effects


@pytest.mark.asyncio
async def test_self_gated_discovery_loads_exact_reviewed_reveal(monkeypatch):
    state, clue, private = fixture_state(gated=True)
    effect = proposal(state, chunks=[private])
    calls = []

    def fake_retrieve(manifests, scene, flags, **kwargs):
        calls.append(flags)
        return {"adventure_context": [clue]}

    monkeypatch.setattr("db.vector.retrieval.retrieve_active_scene", fake_retrieve)
    adj = AdjudicationResult(success=True, story_effects=[effect])
    await prepare_story_validation_state(adj, state)
    accepted = validate_story_effects(adj, state).accepted_story_effects
    assert calls == [{"password_found": "discovered"}]
    assert accepted[0].player_text == clue["text"]
    assert state["adventure_context"] == []  # validator-only until earned
    assert (
        clue["text"]
        not in build_narrator_prompt(
            {**state, "adjudication_result": AdjudicationResult(success=True)}
        )[-1].content
    )


def test_recent_promises_and_committed_discoveries_reach_planning():
    state, _, _ = fixture_state()
    accepted = validate_story_effects(
        AdjudicationResult(success=True, story_effects=[proposal(state)]), state
    ).accepted_story_effects[0]
    state["story_state"]["facts"] = [
        StoryFact(**accepted.model_dump(), fact_id=story_target_id(accepted.target)).model_dump()
    ]
    state["messages"] = [
        HumanMessage(content="I promise to return."),
        AIMessage(content="The guard waits."),
    ]
    prompt = build_adjudicator_prompt(state, None)[-1]["content"]
    assert "I promise to return" in prompt and "The guard waits" in prompt
    assert accepted.effect_id in prompt and "copper dawn" in prompt
    assert "learn_password" in prompt and "Secret future finale" not in prompt


@pytest.mark.asyncio
async def test_rejected_effect_is_not_narrated_as_model_summary(fake_llm_schema):
    state, _, _ = fixture_state()
    bad = proposal(state).model_copy(update={"source_reference_ids": ["fake"]})
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True,
        mechanical_summary="The hidden duke dies and the adventure ends.",
        story_effects=[bad],
    )
    result = await rules_adjudicator_node(state)
    assert not result["adjudication_result"].accepted_story_effects
    assert "hidden duke" not in result["adjudication_result"].mechanical_summary
    assert "hidden duke" not in build_narrator_prompt({**state, **result})[-1].content


def test_static_effect_identity_normalizes_reference_order_mutable_operations_use_turn():
    state, clue, private = fixture_state()
    effect = proposal(state, chunks=[clue, private])
    a = accept_story_effect(effect, turn_id="first", source_refs=[])
    b = accept_story_effect(
        effect.model_copy(
            update={"source_reference_ids": list(reversed(effect.source_reference_ids))}
        ),
        turn_id="second",
        source_refs=[],
    )
    assert a.effect_id == b.effect_id
    effect = effect.model_copy(update={"kind": "relationship", "status": "friendly"})
    assert (
        accept_story_effect(effect, turn_id="first", source_refs=[]).effect_id
        != accept_story_effect(effect, turn_id="second", source_refs=[]).effect_id
    )


def test_absent_optional_fields_keep_legacy_receipt():
    state, _, _ = fixture_state()
    data = deepcopy(state["adventure_manifests"]["module"])
    data.pop("objects")
    data.pop("objectives")
    data.pop("milestones")
    data.pop("promises")
    data.pop("clocks")
    for entity in data["entities"]:
        entity.pop("reveal_on")
    old = AdventureManifest.model_validate(data)
    old.reviewed_sha256 = manifest_digest(old)
    assert require_published(old).reviewed_sha256 == manifest_digest(
        AdventureManifest.model_validate(old.model_dump())
    )


def test_forged_quote_and_legacy_discovery_flag_do_not_persist():
    from agent.models.adjudication import FlagUpdate

    state, clue, _ = fixture_state()
    state["adventure_context"] = [{**clue, "text": "An invented password."}]
    adj = AdjudicationResult(
        success=True,
        story_effects=[proposal(state)],
        flags_set=[FlagUpdate(key="password_found", value="discovered")],
        scene_id="vault",
    )
    validated = validate_story_effects(adj, state)
    assert not validated.accepted_story_effects
    assert validated.flags_set == [] and validated.scene_id == "hall"


def test_allowed_transition_requires_canonical_justified_move():
    state, _, _ = fixture_state()
    state["world_flags"]["key_found"] = "discovered"
    effect = proposal(state, kind="move", target_kind="scene", identity="vault", status="visited")
    validated = validate_story_effects(
        AdjudicationResult(success=True, story_effects=[effect]), state
    )
    assert validated.scene_id == "vault"
    assert validated.accepted_story_effects[0].display_name == "The password is copper dawn"


def test_visit_is_current_only_and_a_turn_accepts_one_move():
    state, _, _ = fixture_state()
    data = state["adventure_manifests"]["module"]
    data["scenes"].append({"scene_id": "garden", "title": "Garden", "links": [], "references": []})
    data["scenes"][0]["links"].append({"scene_id": "garden", "requires": {}})
    manifest = AdventureManifest.model_validate(data)
    manifest.reviewed_sha256 = manifest_digest(manifest)
    state["adventure_manifests"]["module"] = manifest.model_dump()
    for chunk in state["adventure_context"] + state["planner_context"]:
        chunk["manifest_sha256"] = manifest.reviewed_sha256
    state["world_flags"]["key_found"] = "discovered"
    future_visit = proposal(
        state, kind="visit", target_kind="scene", identity="vault", status="visited"
    )
    first_move = proposal(
        state, kind="move", target_kind="scene", identity="vault", status="visited"
    )
    second_move = proposal(
        state, kind="move", target_kind="scene", identity="garden", status="visited"
    )
    adj = validate_story_effects(
        AdjudicationResult(success=True, story_effects=[future_visit, first_move, second_move]),
        state,
    )
    assert [effect.target.canonical_id for effect in adj.accepted_story_effects] == ["vault"]
    assert adj.scene_id == "vault"


def _known_actor_promise_state():
    state, _, _ = fixture_state()
    manifest = state["adventure_manifests"]["module"]
    actor = StoryRef(
        collection="module",
        source_sha256="a" * 64,
        manifest_sha256=manifest["reviewed_sha256"],
        kind="entity",
        canonical_id="mara",
    )
    source = source_citation(state["adventure_context"][0])
    encounter = accept_story_effect(
        StoryEffect(
            kind="encounter",
            target=actor,
            status="encountered",
            source_reference_ids=[source["reference_id"]],
            player_text="Mara greets you.",
        ),
        turn_id="meeting",
        source_refs=[source],
        display_name="Mara",
    )
    fact = StoryFact(**encounter.model_dump(), fact_id=story_target_id(actor), event_id=17)
    state["story_state"]["facts"] = [fact.model_dump()]
    state["player_input"] = "Mara, I promise to bring the note tomorrow."
    candidate = next(c for c in story_candidates(state) if c.get("promise_actor"))
    effect = StoryEffect(
        kind="promise",
        target=StoryRef.model_validate(candidate["target"]),
        status="active",
        promise_actor=actor,
        knowledge_fact_ids=candidate["knowledge_fact_ids"],
        source_reference_ids=candidate["source_reference_ids"],
        player_text="Invented promise.",
    )
    return state, effect


def test_actual_player_promise_is_exact_event_quotation_and_survives_scene_change():
    state, effect = _known_actor_promise_state()
    accepted = validate_story_effects(
        AdjudicationResult(success=True, story_effects=[effect]), state
    ).accepted_story_effects[0]
    quotation = state["player_input"]
    assert accepted.player_text == quotation
    assert accepted.evidence_kind == "player_utterance" and accepted.origin_turn_id == "turn-1"
    assert accepted.display_name == "Player promise"
    fact = StoryFact(**accepted.model_dump(), fact_id=story_target_id(accepted.target), event_id=18)
    state["story_state"]["facts"].append(fact.model_dump())
    state.update(
        current_scene_id="vault",
        adventure_context=[],
        planner_context=[],
        turn_id="turn-2",
        player_input="I deliver the note as promised.",
    )
    candidates = [c for c in story_candidates(state) if c.get("promise_actor")]
    assert candidates[0]["target"] == accepted.target.model_dump()
    fulfilled = effect.model_copy(
        update={"status": "fulfilled", "knowledge_fact_ids": [fact.fact_id]}
    )
    result = validate_story_effects(
        AdjudicationResult(success=True, story_effects=[fulfilled]), state
    )
    done = result.accepted_story_effects[0]
    assert done.player_text == quotation and done.origin_turn_id == "turn-1"
    assert done.turn_id == "turn-2" and done.target == accepted.target
    state["story_state"]["facts"].append(
        StoryFact(**done.model_dump(), fact_id=fact.fact_id, event_id=19).model_dump()
    )
    assert not validate_story_effects(
        AdjudicationResult(success=True, story_effects=[effect, fulfilled]), state
    ).accepted_story_effects


def test_unspoken_or_unanchored_promises_and_failed_fulfillment_rejected():
    state, effect = _known_actor_promise_state()
    absent = {**state, "story_state": {"facts": []}}
    assert not validate_story_effects(
        AdjudicationResult(success=True, story_effects=[effect]), absent
    ).accepted_story_effects
    for utterance in (
        "I don't promise Mara anything.",
        "Mara says I promise to bring a note.",
        "I find a golden sword.",
    ):
        assert not validate_story_effects(
            AdjudicationResult(success=True, story_effects=[effect]),
            {**state, "player_input": utterance},
        ).accepted_story_effects
    forged = effect.model_copy(update={"kind": "discover"})
    assert not validate_story_effects(
        AdjudicationResult(success=True, story_effects=[forged]), state
    ).accepted_story_effects
    active = validate_story_effects(
        AdjudicationResult(success=True, story_effects=[effect]), state
    ).accepted_story_effects[0]
    fact = StoryFact(**active.model_dump(), fact_id=story_target_id(active.target))
    state["story_state"]["facts"].append(fact.model_dump())
    failed_fulfillment = effect.model_copy(
        update={"status": "fulfilled", "knowledge_fact_ids": [fact.fact_id]}
    )
    assert not validate_story_effects(
        AdjudicationResult(success=False, story_effects=[failed_fulfillment]), state
    ).accepted_story_effects


def test_absent_promise_anchor_preserves_existing_effect_hash():
    import hashlib
    import json

    state, _, _ = fixture_state()
    effect = proposal(state)
    legacy_body = effect.model_dump(exclude={"promise_actor"})
    legacy_body["source_reference_ids"] = sorted(set(legacy_body["source_reference_ids"]))
    legacy_body["knowledge_fact_ids"] = sorted(set(legacy_body["knowledge_fact_ids"]))
    expected = hashlib.sha256(
        json.dumps(legacy_body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert accept_story_effect(effect, turn_id="turn", source_refs=[]).effect_id == expected
