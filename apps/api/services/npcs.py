"""NPC data helpers — load campaign NPCs for agent context injection."""

from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import NPC
from edgar_core.story import StoryFact, StoryState, player_story_effect

from .journal_identity import earned_actor_label


async def load_npcs(db: AsyncSession, campaign_id: int, session_id: int | None = None) -> list[NPC]:
    """Return all NPCs for a campaign (lightweight; used by the turn state loader)."""
    result = await db.execute(
        select(NPC).where(
            NPC.campaign_id == campaign_id,
            or_(NPC.session_id.is_(None), NPC.session_id == session_id),
        )
    )
    return list(result.scalars().all())


def model_context_npcs(npcs: Iterable[NPC], story_state: dict) -> list[dict[str, str]]:
    """Project NPC rows for model context without exposing unearned prepared names.

    Manual campaign NPCs and combat-owned names remain useful context. Session story NPCs
    must match a committed encounter/relationship fact; their context name comes from the
    same player-audience text rule as the journal, with a generic label when no name was
    earned. Multiple source revisions of one canonical actor collapse to the latest row.
    """
    rows = list(npcs)
    facts = [StoryFact.model_validate(raw) for raw in StoryState.model_validate(story_state).facts]
    facts_by_id = {fact.fact_id: fact for fact in facts}
    # Resolve labels by canonical actor, since a new reviewed source revision gets a new
    # fact_id while remaining the same person in this session.
    actor_labels: dict[tuple[str, str, str], str] = {}
    actor_keys_by_fact: dict[str, tuple[str, str, str]] = {}
    for fact in facts:
        if fact.kind not in {"encounter", "relationship"} or fact.target.kind != "entity":
            continue
        key = (
            fact.target.collection,
            fact.target.kind,
            fact.target.canonical_id,
        )
        actor_keys_by_fact[fact.fact_id] = key
        public = player_story_effect(fact)
        label = earned_actor_label(fact.display_name, public["text"] if public else None)
        if label:
            actor_labels[key] = label

    latest_actor_rows: dict[tuple[str, str, str], NPC] = {}
    for npc in rows:
        identity = npc.story_identity or ""
        if not identity or identity.startswith("combat:"):
            continue
        fact = facts_by_id.get(identity)
        key = actor_keys_by_fact.get(identity)
        if (
            fact is None
            or key is None
            or fact.kind not in {"encounter", "relationship"}
            or fact.target.kind != "entity"
        ):
            continue
        latest_actor_rows[key] = npc

    # Keep stable input ordering while replacing each actor's stored prepared name with
    # its latest earned label (or a safe generic label).
    projected: list[dict[str, str]] = []
    emitted: set[tuple[str, str, str]] = set()
    for npc in rows:
        identity = npc.story_identity or ""
        if not identity or identity.startswith("combat:"):
            projected.append({"name": npc.name, "disposition": npc.disposition})
            continue
        key = actor_keys_by_fact.get(identity)
        if key is None or key in emitted or latest_actor_rows.get(key) is not npc:
            continue
        emitted.add(key)
        projected.append(
            {
                "name": actor_labels.get(key, "Encountered person"),
                "disposition": npc.disposition,
            }
        )
    return projected
