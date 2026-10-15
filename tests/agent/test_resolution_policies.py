from __future__ import annotations

import random

import pytest

from agent.models.adjudication import AdjudicationResult, CharacterUpdate, FlagUpdate
from agent.models.parsed_input import ParsedInput
from agent.nodes.rules_adjudicator import rules_adjudicator_node
from agent.resolution_policies import exact_recall_fact
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.sources import source_citation
from edgar_core.story import StoryEffect, StoryFact, StoryRef, accept_story_effect
from tools.dice import use_rng


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
    assert "A stout oak door bars the way." in out["policy_narration"]
    assert "impossible" not in out["policy_narration"]


@pytest.mark.asyncio
async def test_invalid_policy_key_is_rejected_without_generic_roll(fake_llm_schema) -> None:
    state = _policy_state(ParsedInput(intent="exploration", resolution_policy_id="module:unknown"))
    out = await rules_adjudicator_node(state)
    assert fake_llm_schema.calls == []
    assert out["adjudication_result"].check is None
    assert out["policy_rejection"]
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
