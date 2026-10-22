"""A resolved view of this turn, derived from validated effects rather than prose."""

from __future__ import annotations

from edgar_core.public_places import public_place_label
from edgar_core.sources import eligible_context
from edgar_core.story import player_story_effect


def resolved_scene(state: dict) -> dict | None:
    """Orient narration at the one accepted destination, or at the unchanged scene.

    This is a staging projection, not another source of truth or a state write. Commit-time
    validation still owns persistence. Only reviewed public arrival text reaches this view.
    """
    if state.get("manifest_status") != "reviewed":
        return None
    from agent.nodes.story_effects import _reviewed_chunk

    adj = state.get("adjudication_result")
    moves = [e for e in getattr(adj, "accepted_story_effects", []) if e.kind == "move"]
    move = moves[-1] if moves else None
    scene_id = move.target.canonical_id if move else state.get("current_scene_id")
    chunks = state.get("story_arrival_context", []) if move else state.get("adventure_context", [])
    public = [
        chunk
        for chunk in eligible_context(chunks, state.get("rules_edition", "2014"))
        if chunk.get("audience") == "player"
        and scene_id in chunk.get("scene_ids", [chunk.get("scene_id")])
        and _reviewed_chunk(chunk, state)
    ]
    arrival = player_story_effect(move) if move else None
    texts = list(dict.fromkeys(c["text"] for c in public if c.get("text")))
    if not texts and arrival and arrival.get("text"):
        texts = [arrival["text"]]
    label = public_place_label("\n".join(texts), (arrival or {}).get("name", ""))
    return {
        "scene_id": scene_id,
        "previous_scene_id": state.get("current_scene_id"),
        "moved": bool(move),
        "label": label or "Current location",
        "public_context": public,
        "visible_description": texts,
    }
