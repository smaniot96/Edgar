"""Current source evidence reaches the parser and earned answers reach narration.

Reading scene guidance is local retrieval, not another semantic decision. The
existing consequence planner remains the DM who judges what an action earns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from edgar_core.manifests import require_published


def scene_signature(state):
    payload = {
        "scene": state.get("current_scene_id"),
        "flags": state.get("world_flags") or {},
        "edition": state.get("rules_edition", "2014"),
        "manifests": {
            key: value.get("reviewed_sha256")
            for key, value in (state.get("adventure_manifests") or {}).items()
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def cached_scene_context(state):
    cache = state.get("parser_scene_context") or {}
    return cache.get("context") if cache.get("signature") == scene_signature(state) else None


async def preload_scene_context(state):
    """Read the current scene once, then reuse that exact verified selection."""
    from db.vector.retrieval import retrieve_active_scene

    if state.get("source_mode") != "module" or not state.get("adventure_manifests"):
        return None
    if cached_scene_context(state) is not None:
        return state["parser_scene_context"]
    context = await asyncio.to_thread(
        retrieve_active_scene,
        state["adventure_manifests"],
        state.get("current_scene_id"),
        state.get("world_flags"),
        client=state.get("qdrant_client"),
        rules_edition=state.get("rules_edition", "2014"),
    )
    transitions = []
    # Only expose the linked opportunity's trigger, never its private facts/source.
    # Exact retrieval still checks hashes; these references authorize no observations.
    for destination in context.get("allowed_scene_ids", []):
        if destination == state.get("current_scene_id"):
            continue
        linked = await asyncio.to_thread(
            retrieve_active_scene,
            state["adventure_manifests"],
            destination,
            state.get("world_flags"),
            client=state.get("qdrant_client"),
            rules_edition=state.get("rules_edition", "2014"),
        )
        for chunk in linked.get("planner_context", []):
            if chunk.get("scene_ids") != [destination]:
                continue
            paragraphs = chunk.get("text", "").split("\n\n")
            if len(paragraphs) < 3 or not paragraphs[1].startswith("Trigger: "):
                continue
            transitions.append(
                {
                    "scene_id": destination,
                    "title": paragraphs[0].removeprefix("# "),
                    "trigger": paragraphs[1].removeprefix("Trigger: "),
                    "timing": paragraphs[2].removeprefix("Timing: "),
                }
            )
            break
    return {
        "signature": scene_signature(state),
        "context": {**context, "manifest_status": "reviewed"},
        "event_transitions": transitions,
    }


def parser_guidance(state):
    """Bound parser context to current event notes and chapter running guidance."""
    context = cached_scene_context(state)
    if context is None:
        return ""
    chunks = [
        c
        for c in context.get("planner_context", [])
        if c.get("passage_id", "").endswith(("_dm", "_content"))
        and state.get("current_scene_id") in c.get("scene_ids", [])
    ]
    chunks.sort(key=lambda c: not c.get("passage_id", "").endswith("_dm"))
    text = "\n\n".join(c.get("text", "") for c in chunks)
    if not text:
        return ""
    return (
        "Private reviewed current-scene DM guidance (classification evidence only; never disclose it):\n"
        + text[:8000]
        + "\nRespect source instructions that ordinary questions, visible details and accessible searches need no check. "
        "An unknown answer is not itself uncertainty requiring a roll. Never invent a check to delay an ordinary answer."
    )


def active_observation_chunks(state):
    """Only active scene facts; shared campaign registries cannot authorize disclosure."""
    from agent.nodes.story_effects import _reviewed_chunk
    from edgar_core.manifests import select_scene_context
    from edgar_core.sources import eligible_context

    current = state.get("current_scene_id")
    edition = state.get("rules_edition", "2014")
    result = []
    for chunk in eligible_context(
        state.get("adventure_context", []) + state.get("planner_context", []), edition
    ):
        if chunk.get("passage_id") in ("campaign_registry", "campaign_spine"):
            continue
        value = (state.get("adventure_manifests") or {}).get(chunk.get("collection"))
        if (
            value is None
            or current not in chunk.get("scene_ids", [])
            or not _reviewed_chunk(chunk, state)
        ):
            continue
        manifest = require_published(value)
        selected = select_scene_context(manifest, current, state.get("world_flags"))
        if (
            chunk.get("passage_id")
            in selected["player_passage_ids"] + selected["planner_passage_ids"]
        ):
            result.append(chunk)
    return result


def validate_source_observations(adjudication, state):
    """Seal exact grounded answers chosen by the existing live DM judgment stage.

    Narrative appropriateness is the DM's semantic judgment, as for goal completion.
    Code enforces its boundaries: current source, exact quote/action, successful
    attempt, no future/global dossier and no mechanical or persistent state writes.
    """
    from edgar_core.sources import source_citation

    adjudication.accepted_observations = []
    diagnostics = {
        "proposed": len(adjudication.source_observations),
        "accepted": 0,
        "eligible_references": 0,
        "rejected": [],
    }
    adjudication.observation_diagnostics = diagnostics
    if (
        state.get("manifest_status") != "reviewed"
        or not state.get("turn_id")
        or not adjudication.success
    ):
        diagnostics["blocked"] = "turn_not_eligible"
        return
    evidence = {
        source_citation(c, state.get("rules_edition", "2014"))["reference_id"]: c
        for c in active_observation_chunks(state)
    }
    diagnostics["eligible_references"] = len(evidence)
    action = state.get("player_input", "")
    for index, proposal in enumerate(adjudication.source_observations):
        chunk = evidence.get(proposal.source_reference_id)
        reason = None
        if not chunk:
            reason = "ineligible_source_reference"
        elif not proposal.achievement_basis.strip():
            reason = "missing_achievement_basis"
        elif not proposal.action_quote.strip() or proposal.action_quote not in action:
            reason = "action_quote_not_exact"
        elif not proposal.text.strip() or proposal.text not in chunk["text"]:
            reason = "source_quote_not_exact"
        if reason:
            diagnostics["rejected"].append({"index": index, "reason": reason})
            continue
        if proposal.text not in adjudication.accepted_observations:
            adjudication.accepted_observations.append(proposal.text)
    diagnostics["accepted"] = len(adjudication.accepted_observations)
