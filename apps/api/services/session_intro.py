"""Opening-scene narration for a fresh session ("session zero").

A new session otherwise starts as a blank chat and the player has no idea what to do. This
service generates a grounded opening: the DM sets the scene from the adventure module and
invites the player to act. It is persisted as the session's first narration event, so it also
shows up first when the player later *continues* the campaign (full history).

Idempotent: if the session already has any narration, this is a no-op (returns None).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.llm import make_chat_model
from db.postgres.models import Campaign, EventLog, Session as SessionModel
from db.vector.client import get_qdrant_client
from db.vector.retrieval import search_adventure_context

from .character_assignments import character_play_state

log = structlog.get_logger()

_INTRO_TEMPERATURE = 0.7

INTRO_SYSTEM = (
    "You are the Dungeon Master opening a brand-new session of a Dungeons & Dragons 5e "
    "adventure. Write a vivid, welcoming opening of 2-4 short paragraphs that pulls the player "
    "in. Ground it strictly in the provided adventure-module context: establish where the "
    "player's character is, the situation and hook that begins the story, and the atmosphere. "
    "Address the character by name. End with a short, open invitation to act (for example, "
    "'What do you do?'). Do NOT roll dice, resolve actions, introduce the player's decisions, "
    "or skip ahead — just set the stage."
)

_INTRO_QUERY = (
    "The adventure begins: opening scene and read-aloud introduction. Where do the player "
    "characters start, what is the hook that draws them in, the initial location, mood, and "
    "the situation at the very start of the adventure."
)


_PAGE_WINDOWS = (2, 5, 12, 30, 80)
_page_index_attempted: set[str] = set()


def _payload(p: object) -> dict:
    return getattr(p, "payload", None) or {}


def _sort_key(p: object) -> tuple[int, int]:
    payload = _payload(p)
    page = payload.get("page")
    idx = payload.get("chunk_index")
    return (
        page if isinstance(page, int) else 10_000,
        idx if isinstance(idx, int) else 0,
    )


def _scroll_ordered_by_page(client, collection: str, limit: int) -> list:
    """First `limit` points by ascending page via `order_by` (needs a `page` payload index).

    The index is created lazily, once per collection and process; it only adds an index and
    never touches points. Any failure returns [] so the caller can use the range-filter path.
    """
    from qdrant_client.models import OrderBy, PayloadSchemaType

    def _scroll():
        points, _ = client.scroll(
            collection_name=collection,
            limit=limit,
            order_by=OrderBy(key="page", direction="asc"),
            with_payload=True,
            with_vectors=False,
        )
        return points

    try:
        return _scroll()
    except Exception as e:
        if collection in _page_index_attempted:
            log.warning("intro_order_by_failed", collection=collection, error=type(e).__name__)
            return []
    _page_index_attempted.add(collection)
    try:
        client.create_payload_index(
            collection_name=collection,
            field_name="page",
            field_schema=PayloadSchemaType.INTEGER,
            wait=True,
        )
        return _scroll()
    except Exception as e:
        log.warning("intro_page_index_unavailable", collection=collection, error=type(e).__name__)
        return []


def _scroll_page_window(client, collection: str, max_chunks: int) -> list:
    """Fallback without an index: widen a `page <= N` range filter until enough points appear."""
    from qdrant_client.models import FieldCondition, Filter, Range

    points: list = []
    for last_page in _PAGE_WINDOWS:
        try:
            points, _ = client.scroll(
                collection_name=collection,
                scroll_filter=Filter(must=[FieldCondition(key="page", range=Range(lte=last_page))]),
                limit=256,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            log.warning("intro_scroll_failed", collection=collection, error=type(e).__name__)
            return []
        if len(points) >= max_chunks:
            break
    return points


def _fetch_opening_text(adventure_collections: list[str], max_chunks: int = 7) -> str:
    """The literal start of the module: earliest-page chunks of the primary collection.

    Semantic search returns the most *similar* text, which for an "opening" query is often a
    dramatic mid-adventure scene. The actual opening (premise + read-aloud arrival) lives on the
    first pages, so we read those directly and let the DM narrate from them. Point ids are
    hashes, so an unfiltered scroll returns arbitrary pages; we order by the `page` payload
    (payload index + `order_by`), or fall back to a widening `page <= N` range filter.
    """
    if not adventure_collections:
        return ""
    client = get_qdrant_client()
    collection = adventure_collections[0]

    # Over-fetch so chunks sharing the first page can be put back in reading order.
    points = _scroll_ordered_by_page(client, collection, limit=max_chunks * 4)
    if not points:
        points = _scroll_page_window(client, collection, max_chunks)

    points = sorted(points, key=_sort_key)
    texts: list[str] = []
    for p in points[:max_chunks]:
        t = _payload(p).get("text", "")
        if t:
            texts.append(t[:700])
    return "\n".join(texts)


async def _has_narration(db: AsyncSession, session_id: int) -> bool:
    r = await db.execute(
        select(EventLog.id)
        .where(EventLog.session_id == session_id, EventLog.event_type == "narration")
        .limit(1)
    )
    return r.scalar_one_or_none() is not None


async def generate_session_intro(db: AsyncSession, session: SessionModel) -> dict[str, Any] | None:
    """Generate + persist the opening scene if the session has none yet. Returns a payload
    shaped like a turn's `done` event, or None if the session already started."""
    if await _has_narration(db, session.id):
        return None

    camp_res = await db.execute(select(Campaign).where(Campaign.id == session.campaign_id))
    campaign = camp_res.scalar_one_or_none()
    adventure_collections = list(campaign.adventure_collections or []) if campaign else []

    character = None
    if session.active_character_id:
        character = await character_play_state(db, session.active_character_id, session.campaign_id)

    # Prefer the literal start of the module; fall back to semantic search if scroll is empty.
    opening_text = await asyncio.to_thread(_fetch_opening_text, adventure_collections)
    if not opening_text:
        hits = await asyncio.to_thread(
            search_adventure_context,
            _INTRO_QUERY,
            adventure_collections,
            campaign_id=session.campaign_id,
            limit_per_collection=6,
        )
        opening_text = "\n".join(c.get("text", "") for c in hits[:6])

    context_parts: list[str] = []
    if character:
        context_parts.append(
            f"The player's character: {character['name']}, a level {character['level']} "
            f"{character['class']}."
        )
    if opening_text:
        context_parts.append(
            "This is the very beginning of the adventure module. Open the story HERE, at the "
            "start — do not skip ahead to later scenes:\n" + opening_text
        )
    else:
        context_parts.append(
            "No adventure text was found; write a brief, generic but inviting opening for a "
            "fresh D&D adventure and prompt the player to set out."
        )
    context_parts.append("Write the opening narration now.")

    llm = make_chat_model(temperature=_INTRO_TEMPERATURE)
    try:
        response = await llm.ainvoke(
            [SystemMessage(content=INTRO_SYSTEM), HumanMessage(content="\n\n".join(context_parts))]
        )
    except Exception:
        # Never block play on a flaky opening; the player can still type a first action.
        return None

    raw = response.content if hasattr(response, "content") else str(response)
    narration = raw if isinstance(raw, str) else str(raw)
    if not narration.strip():
        return None

    # Persist as the first narration event (empty player_input => renders as a DM-only message).
    db.add(
        EventLog(
            session_id=session.id,
            event_type="narration",
            payload={"player_input": "", "narration": narration},
        )
    )
    await db.commit()

    return {
        "narration": narration,
        "character": character,
        "current_scene_id": session.current_scene_id,
    }
