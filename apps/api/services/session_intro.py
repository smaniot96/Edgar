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

from fastapi import HTTPException
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.llm import make_chat_model, message_text
from db.postgres.models import Campaign, EventLog, Session as SessionModel
from db.vector.retrieval import retrieve_active_scene
from edgar_core.manifests import ManifestError
from edgar_core.sources import (
    DEFAULT_RULES_EDITION,
    citations_for,
    cited_excerpt,
    public_citations,
)

from . import adventure_manifests
from .campaign_sources import require_campaign_sources, source_error
from .character_assignments import character_play_state

_INTRO_TEMPERATURE = 0.7

INTRO_SYSTEM = (
    "You are the Dungeon Master opening a brand-new session of a Dungeons & Dragons 5e "
    "adventure. Write a vivid, welcoming opening of 2-4 short paragraphs that pulls the player "
    "in. When adventure-module text is provided, ground it strictly in that context; when the "
    "player explicitly chose homebrew, improvise an opening. Establish where the "
    "player's character is, the situation and hook that begins the story, and the atmosphere. "
    "Address the character by name. End with a short, open invitation to act (for example, "
    "'What do you do?'). Do NOT roll dice, resolve actions, introduce the player's decisions, "
    "or skip ahead — just set the stage."
)


def _reviewed_opening(primary: str, edition: str) -> tuple[str, list[dict]]:
    """The first binding owns the opening; secondary modules cannot replace its selection."""
    manifests = adventure_manifests.campaign_manifests([primary])
    scene_id = manifests[primary]["opening_scene_id"]
    context = retrieve_active_scene(manifests, scene_id, {}, rules_edition=edition)
    passages = context["adventure_context"]
    if not passages:
        raise ManifestError(
            "Choose an opening scene with reviewed player text compatible with the campaign edition."
        )
    return scene_id, passages


async def _has_narration(db: AsyncSession, session_id: int) -> bool:
    r = await db.execute(
        select(EventLog.id)
        .where(EventLog.session_id == session_id, EventLog.event_type == "narration")
        .limit(1)
    )
    return r.scalar_one_or_none() is not None


async def generate_session_intro(db: AsyncSession, session: SessionModel) -> dict[str, Any] | None:
    """Stage a fresh opening in the caller's transaction; return its public payload.

    Existing narration, an unavailable model or an empty reply returns None without writes.
    """
    if await _has_narration(db, session.id):
        return None

    camp_res = await db.execute(select(Campaign).where(Campaign.id == session.campaign_id))
    campaign = camp_res.scalar_one_or_none()
    if campaign is None:
        return None
    sources = await require_campaign_sources(campaign)
    homebrew = campaign.source_mode == "homebrew"
    adventure_collections = [] if homebrew else list(campaign.adventure_collections or [])

    character = None
    if session.active_character_id:
        character = await character_play_state(db, session.active_character_id, session.campaign_id)

    edition = getattr(campaign, "rules_edition", None) or DEFAULT_RULES_EDITION
    opening_scene_id = None
    passages: list[dict] = []
    if not homebrew:
        try:
            opening_scene_id, passages = await asyncio.to_thread(
                _reviewed_opening, adventure_collections[0], edition
            )
        except ManifestError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "adventure_manifest_review_required",
                    "message": str(exc),
                    "source": adventure_collections[0],
                },
            ) from exc
        except Exception as exc:
            for source in sources:
                source.status = "unavailable"
                source.message = (
                    "Reviewed adventure text could not be retrieved for the opening. "
                    "Try again or explicitly choose homebrew play."
                )
            raise source_error(sources) from exc

    citations = public_citations(citations_for(passages, edition))
    opening_text = "\n\n".join(cited_excerpt(p, edition) for p in passages)

    context_parts: list[str] = []
    if character:
        context_parts.append(
            f"The player's character: {character['name']}, a level {character['level']} "
            f"{character['class']}."
        )
    if opening_text:
        context_parts.append(
            "Open the story at the reviewed opening scene. "
            "Use only these complete player-approved passages; citation labels identify their "
            "sources and must not be narrated. Do not skip ahead to later scenes:\n" + opening_text
        )
    else:
        context_parts.append(
            "The player explicitly chose homebrew play. Write a brief, inviting opening for a "
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

    narration = message_text(response)
    if not narration.strip():
        return None

    if opening_scene_id is not None:
        session.current_scene_id = opening_scene_id

    # Persist as the first narration event (empty player_input => renders as a DM-only message).
    db.add(
        EventLog(
            session_id=session.id,
            event_type="narration",
            payload={
                "player_input": "",
                "narration": narration,
                "current_scene_id": session.current_scene_id,
                "citations": citations,
                "rules_edition": edition,
            },
        )
    )

    return {
        "narration": narration,
        "character": character,
        "current_scene_id": session.current_scene_id,
        "citations": citations,
        "rules_edition": edition,
    }
