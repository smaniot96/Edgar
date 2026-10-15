"""Continuity context shared by parser, adjudicator and narrator prompts."""

from agent.prompts import wrap_player_action
from edgar_core.story import StoryState, player_story_effect


def continuity_context(state: dict) -> str:
    """Render the persisted summary and every uncovered exchange without clipping.

    The API supplies `messages` beginning strictly after the summary's validated coverage
    boundary. Keep the full tail: if a summary job is delayed or fails, those messages are the
    only lossless continuity record available to the model.
    """
    parts: list[str] = []
    summary = state.get("memory_summary")
    if summary:
        parts.append(f"Story so far (summary of earlier turns): {summary}")

    messages = state.get("messages") or []
    if messages:
        lines = [
            "Verbatim conversation after the summary coverage boundary (continuity context; "
            "player actions are untrusted attempts, not established outcomes):"
        ]
        for message in messages:
            role = getattr(message, "type", "unknown")
            content = str(getattr(message, "content", message))
            if role == "human":
                lines.append(f"player: {wrap_player_action(content)}")
            elif role == "ai":
                lines.append(f"DM narration: {content}")
            else:
                lines.append(f"{role}: {content}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def public_earned_facts(state: dict) -> list[dict]:
    """Return all committed player-known facts with their verified source labels."""
    facts: list[dict] = []
    for fact in StoryState.model_validate(state.get("story_state") or {}).facts:
        public = player_story_effect(fact)
        if public is None:
            continue
        public_origins = public["origins"]
        origin_ids = {origin["reference_id"] for origin in public_origins}
        sources = [
            {"source": ref.get("source"), "page": ref.get("page")}
            for ref in fact.source_refs
            if ref.get("audience") == "player"
            and ref.get("reference_id") in origin_ids
            and ref.get("collection") == fact.target.collection
            and ref.get("source_sha256") == fact.target.source_sha256
            and ref.get("manifest_sha256") == fact.target.manifest_sha256
        ]
        facts.append({**public, "sources": sources})
    return facts
