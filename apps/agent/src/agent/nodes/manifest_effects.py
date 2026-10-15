"""Constrain structured scene/discovery proposals after engine-owned adjudication."""

from agent.models.adjudication import AdjudicationResult
from agent.state import AgentState
from edgar_core.manifests import require_published


def enforce_manifest_effects(adj: AdjudicationResult, state: AgentState) -> AdjudicationResult:
    if state.get("manifest_status") != "reviewed":
        return adj
    known_flags = {
        entity.discovery_flag
        for value in (state.get("adventure_manifests") or {}).values()
        for entity in require_published(value).entities
        if entity.discovery_flag
    }
    relevant_flags = {
        entity["discovery_flag"]
        for entity in state.get("discovery_entities", [])
        if entity.get("discovery_flag")
    }
    return adj.model_copy(
        update={
            "scene_id": adj.scene_id
            if adj.scene_id in state.get("allowed_scene_ids", [])
            else state.get("current_scene_id"),
            "flags_set": [
                flag
                for flag in adj.flags_set
                if flag.key not in known_flags
                or (flag.key in relevant_flags and flag.value == "discovered" and adj.success)
            ],
            # A discovery is durable campaign knowledge; changing scene cannot erase it.
            "flags_cleared": [key for key in adj.flags_cleared if key not in known_flags],
        }
    )
