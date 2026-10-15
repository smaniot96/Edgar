"""Validate explicit reviewed opening seeds through the normal story-effect boundary."""

from agent.models.adjudication import AdjudicationResult
from agent.nodes.story_effects import validate_story_effects
from edgar_core.manifests import AdventureManifest, ManifestError, require_published
from edgar_core.sources import source_citation
from edgar_core.story import AcceptedStoryEffect, StoryEffect, StoryRef


def opening_journal_effects(
    collection: str,
    manifest: AdventureManifest | dict,
    passages: list[dict],
    edition: str,
) -> list[AcceptedStoryEffect]:
    """No provider or writes. Missing/stale/private evidence cannot become a seed."""
    reviewed = require_published(manifest, {"rules_edition": edition})
    proposals = []
    for seed in reviewed.opening_journal:
        supporting = [p for p in passages if p.get("passage_id") in seed.passage_ids]
        if {p.get("passage_id") for p in supporting} != set(seed.passage_ids):
            raise ManifestError("Reviewed opening journal evidence is unavailable.")
        target_kind = {
            "discover": "entity",
            "encounter": "entity",
            "visit": "scene",
            "learn_place": "scene",
        }.get(seed.kind, seed.kind)
        proposals.append(
            StoryEffect(
                kind=seed.kind,
                target=StoryRef(
                    collection=collection,
                    source_sha256=reviewed.source_sha256,
                    manifest_sha256=reviewed.reviewed_sha256,
                    kind=target_kind,
                    canonical_id=seed.target_id,
                ),
                status=seed.status,
                source_reference_ids=[
                    source_citation(p, edition)["reference_id"] for p in supporting
                ],
            )
        )
    result = validate_story_effects(
        AdjudicationResult(success=True, story_effects=proposals),
        {
            "manifest_status": "reviewed",
            "turn_id": "opening:" + reviewed.reviewed_sha256,
            "adventure_manifests": {collection: reviewed.model_dump()},
            "current_scene_id": reviewed.opening_scene_id,
            "world_flags": {},
            "story_state": {"facts": []},
            "adventure_context": passages,
            "rules_edition": edition,
        },
    )
    if len(result.accepted_story_effects) != len(proposals):
        raise ManifestError("Opening journal seeds require matching reviewed player evidence.")
    return result.accepted_story_effects
