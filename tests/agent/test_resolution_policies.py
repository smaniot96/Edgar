from __future__ import annotations

import random

import pytest

from agent.models.adjudication import AdjudicationResult, CharacterUpdate, FlagUpdate
from agent.models.parsed_input import ParsedInput
from agent.nodes.narrator import deterministic_narration
from agent.nodes.rules_adjudicator import rules_adjudicator_node
from agent.resolution_policies import exact_recall_fact
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.sources import source_citation
from edgar_core.story import StoryEffect, StoryFact, StoryRef, accept_story_effect
from tools.dice import use_rng


def test_failed_empty_policy_keeps_public_scene_narration_available():
    state = {
        "policy_narration": "The check fails. That approach produces no further change.",
        "adjudication_result": AdjudicationResult(success=False),
    }
    assert deterministic_narration(state) is None
    # A rejected unavailable policy still has its exact refusal, and success keeps
    # the existing authored-result path rather than inventing another outcome.
    state["policy_rejection"] = True
    assert deterministic_narration(state) == "That approach is not available in the current circumstances."
    state.pop("policy_rejection")
    state["adjudication_result"] = AdjudicationResult(success=True)
    assert deterministic_narration(state) == state["policy_narration"]


def _policy_state(parsed: ParsedInput | None = None) -> dict:
    text = "A stout oak door bars the way."
    manifest = AdventureManifest.model_validate(
        {
            "source": "module.pdf",
            "source_sha256": "a" * 64,
            "extraction_sha256": "b" * 64,
            "rules_edition": "2014",
            "opening_scene_id": "hall",
            "scenes": [{"scene_id": "hall", "title": "Hall"}],
            "passages": [
                {
                    "passage_id": "door",
                    "page": 1,
                    "start": 0,
                    "end": len(text),
                    "text_sha256": text_digest(text),
                    "scene_ids": ["hall"],
                    "audience": "player",
                }
            ],
            "objectives": [
                {
                    "canonical_id": "open_door",
                    "title": "Open the door",
                    "scene_ids": ["hall"],
                    "passage_ids": ["door"],
                    "statuses": ["completed"],
                }
            ],
            "resolution_policies": [
                {
                    "policy_id": "force_door",
                    "action": "Force open the oak door",
                    "scene_ids": ["hall"],
                    "passage_ids": ["door"],
                    "mode": "check",
                    "check": "athletics",
                    "dc": 13,
                    "failure_mode": "no_progress",
                    "on_success": [
                        {
                            "kind": "objective",
                            "target_id": "open_door",
                            "status": "completed",
                            "passage_ids": ["door"],
                        }
                    ],
                }
            ],
            "reviewed_by": "fixture",
        }
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    chunk = {
        "collection": "module",
        "source": "module.pdf",
        "source_sha256": manifest.source_sha256,
        "extraction_sha256": manifest.extraction_sha256,
        "manifest_sha256": manifest.reviewed_sha256,
        "rules_edition": "2014",
        "kind": "adventure",
        "page": 1,
        "scene_id": "hall",
        "passage_id": "door",
        "text": text,
        "text_sha256": text_digest(text),
        "audience": "player",
    }
    return {
        "player_input": "I brace and force the oak door open.",
        "parsed_input": parsed
        or ParsedInput(intent="exploration", resolution_policy_id="module:force_door"),
        "character": {
            "id": 1,
            "name": "Eda",
            "class": "Fighter",
            "level": 1,
            "hp_current": 12,
            "hp_max": 12,
            "stats": {"STR": 16, "DEX": 12, "CON": 14, "INT": 10, "WIS": 12, "CHA": 8},
            "inventory": {},
        },
        "world_flags": {},
        "current_scene_id": "hall",
        "allowed_scene_ids": ["hall"],
        "rules_edition": "2014",
        "source_mode": "module",
        "manifest_status": "reviewed",
        "adventure_manifests": {"module": manifest.model_dump()},
        "adventure_context": [chunk],
        "planner_context": [],
        "story_state": {"facts": []},
        "turn_id": "turn-1",
    }


@pytest.mark.asyncio
async def test_policy_uses_authored_dc_and_deterministic_narration(fake_llm_schema) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=False,
        mechanical_summary="The impossible door explodes into gold.",
        flags_set=[FlagUpdate(key="invented", value="true")],
        character_update=CharacterUpdate(hp_delta=10),
    )
    with use_rng(random.Random(0)):
        out = await rules_adjudicator_node(_policy_state())

    result = out["adjudication_result"]
    assert result.dc == 13 and result.check == "athletics" and result.success
    assert result.flags_set == [] and result.character_update is None
    assert len(result.accepted_story_effects) == 1
    assert out["policy_narration"].startswith("Your approach succeeds.")
    assert "completed" in out["policy_narration"]
    assert "A stout oak door bars the way." not in out["policy_narration"]
    assert "impossible" not in out["policy_narration"]


@pytest.mark.asyncio
async def test_invalid_policy_key_is_rejected_without_generic_roll(fake_llm_schema) -> None:
    state = _policy_state(ParsedInput(intent="exploration", resolution_policy_id="module:unknown"))
    out = await rules_adjudicator_node(state)
    assert fake_llm_schema.calls == []
    assert out["adjudication_result"].check is None
    assert out["policy_rejection"]


def test_reread_keeps_the_earned_public_quote_after_manifest_is_unavailable() -> None:
    state = _policy_state(ParsedInput(intent="exploration", recall_fact_id=None))
    target = StoryRef(
        collection="module",
        source_sha256="a" * 64,
        manifest_sha256=state["adventure_manifests"]["module"]["reviewed_sha256"],
        kind="entity",
        canonical_id="old_clue",
    )
    effect = StoryEffect(
        kind="discover",
        target=target,
        status="discovered",
        player_text="The guard said: 'At dawn, cross the river.'",
    )
    chunk = state["adventure_context"][0]
    sealed = accept_story_effect(
        effect,
        turn_id="earlier-turn",
        source_refs=[source_citation(chunk)],
        display_name="The guard's warning",
    )
    fact = StoryFact(**sealed.model_dump(), fact_id="f" * 64)
    state["story_state"] = {"facts": [fact.model_dump()]}
    state["adventure_manifests"] = {}
    state["player_input"] = "Read the clue again."
    state["parsed_input"] = ParsedInput(intent="exploration")

    recall = exact_recall_fact(state, state["parsed_input"])
    assert recall is not None
    assert recall["text"] == effect.player_text
    assert recall["citation"]["reference_id"] == source_citation(chunk)["reference_id"]


def test_empty_policy_extension_preserves_pre_extension_review_digest():
    # Recorded using the core implementation at 2994eaf, before resolution_policies existed.
    manifest = AdventureManifest(
        source="legacy",
        source_sha256="a" * 64,
        extraction_sha256="b" * 64,
        opening_scene_id="hall",
        scenes=[{"scene_id": "hall", "title": "Hall"}],
        passages=[
            {
                "passage_id": "hall",
                "page": 1,
                "start": 0,
                "end": 4,
                "scene_ids": ["hall"],
                "audience": "player",
                "text_sha256": "c" * 64,
            }
        ],
    )
    assert (
        manifest_digest(manifest)
        == "9a7a2318d0ac05e6ac5050a0044c28142a1baf25bb894eb5687f7f6842b36f69"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "text", "edition", "receipt"])
async def test_policy_rejects_stale_or_wrong_edition_evidence(fake_llm_schema, change):
    state = _policy_state()
    if change == "source":
        state["adventure_context"][0]["source_sha256"] = "f" * 64
    elif change == "text":
        state["adventure_context"][0]["text"] = "Forged source DC 1 and gold."
    elif change == "edition":
        state["rules_edition"] = "2024"
    else:
        state["adventure_manifests"]["module"]["resolution_policies"][0]["dc"] = 1
    out = await rules_adjudicator_node(state)
    assert out["policy_rejection"]
    assert out["adjudication_result"].check is None
    assert out["adjudication_result"].accepted_story_effects == []


@pytest.mark.asyncio
async def test_public_reading_uses_automatic_policy_without_model_or_roll(fake_llm_schema):
    state = _policy_state()
    raw = state["adventure_manifests"]["module"]
    raw["entities"] = [
        {"entity_id": "sign", "kind": "clue", "title": "Hall sign", "discovery_flag": "sign_known"}
    ]
    raw["passages"][0]["entity_ids"] = ["sign"]
    raw["resolution_policies"] = [
        {
            "policy_id": "read_sign",
            "action": "Read the public sign",
            "scene_ids": ["hall"],
            "passage_ids": ["door"],
            "mode": "automatic",
            "on_automatic": [
                {
                    "kind": "discover",
                    "target_id": "sign",
                    "status": "discovered",
                    "passage_ids": ["door"],
                }
            ],
        }
    ]
    manifest = AdventureManifest.model_validate(raw)
    manifest.reviewed_sha256 = manifest_digest(manifest)
    state["adventure_manifests"]["module"] = manifest.model_dump()
    state["adventure_context"][0]["manifest_sha256"] = manifest.reviewed_sha256
    state["parsed_input"] = ParsedInput(
        intent="exploration", check="history", resolution_policy_id="module:read_sign"
    )
    out = await rules_adjudicator_node(state)
    result = out["adjudication_result"]
    assert result.success and result.check is None and result.dice_result is None
    assert len(result.accepted_story_effects) == 1
    assert result.accepted_story_effects[0].player_text == state["adventure_context"][0]["text"]
    assert fake_llm_schema.calls == []


@pytest.mark.asyncio
async def test_rejected_consequences_preserve_the_engine_check_verdict(
    fake_llm_schema, monkeypatch
):
    monkeypatch.setattr(
        "agent.nodes.rules_adjudicator.validate_story_effects", lambda result, state: result
    )
    with use_rng(random.Random(0)):
        out = await rules_adjudicator_node(_policy_state())
    result = out["adjudication_result"]
    assert out["policy_rejection"] and result.accepted_story_effects == []
    assert result.dc == 13 and result.dice_result >= 13 and result.success
