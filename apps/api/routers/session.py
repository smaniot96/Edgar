"""Session router: CRUD plus the two turn endpoints (sync JSON and SSE stream).

Both turn endpoints take the per-session turn lock FIRST, then build the agent's initial state
with `_load_turn_initial_state` (so the state they act on cannot be stale), run the same agent
topology, and persist through `services.turn_runner.persist_turn` (one transaction per turn).
The rolling memory summary is updated as a background task after the response.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Response
from fastapi.responses import JSONResponse, StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from agent.combat_telemetry import COMBAT_ROUND_EVENT, combat_round_public
from db.postgres.models import Campaign, Character, EventLog, Session as SessionModel
from edgar_core.sources import public_citations

from ..dependencies import acquire_turn_lock, get_redis, get_session
from ..schemas.session import ChatMessageRead, SessionCreate, SessionRead, SessionUpdate
from ..schemas.turn import TurnRequest, TurnResponse, debug_turns_enabled
from ..services.campaign_sources import source_reads
from ..services.character_assignments import character_play_state, load_active_assignment
from ..services.combat import (
    load_active_combat,
    load_combat_captives,
    load_resolved_combat_outcomes,
)
from ..services.npcs import load_npcs, model_context_npcs
from ..services.roll_record import stored_roll
from ..services.safe_errors import classify_error, classify_http_failure, log_failure
from ..services.session_intro import generate_session_intro
from ..services.session_messages import (
    load_session_context,
    maybe_update_memory_summary,
)
from ..services.story_state import load_story_state, session_story_flags
from ..services.turn_receipts import load_turn_receipt
from ..services.turn_runner import (
    TurnError,
    combat_round_from_events,
    combat_state_public,
    persist_turn,
    public_adjudication,
    run_turn,
    sse_event,
    stream_session_turn,
)
from ..services.world_flags import load_world_flags
from ._common import get_or_404

log = structlog.get_logger()

_BUSY_DETAIL = "A turn is already in progress for this session. Please retry."

router = APIRouter(tags=["sessions"])


async def _session_read(session: SessionModel, db: AsyncSession) -> SessionRead:
    """Project a save with its current campaign and assignment readiness."""
    value = SessionRead.model_validate(session)
    last_played_at = await db.scalar(
        select(func.max(EventLog.created_at)).where(
            EventLog.session_id == session.id,
            EventLog.event_type == "narration",
        )
    )
    character = (
        await db.get(Character, session.active_character_id)
        if session.active_character_id is not None
        else None
    )
    campaign = await db.get(Campaign, session.campaign_id)
    if campaign is None:
        from ..schemas.readiness import ReadinessBlocker, ReadinessRead

        blocker = ReadinessBlocker(
            code="readiness_unavailable",
            message="Campaign readiness could not be checked. Retry before starting a turn.",
            action="retry_readiness",
        )
        readiness = ReadinessRead(
            play_allowed=False,
            state=blocker.code,
            primary_action=blocker.action,
            blockers=[blocker],
        )
    else:
        from ..services.campaign_readiness import evaluate_play_readiness

        readiness = await evaluate_play_readiness(
            db,
            campaign,
            character_id=session.active_character_id,
            require_character=True,
            session_id=session.id,
        )
    return value.model_copy(
        update={
            "readiness": readiness,
            "has_history": last_played_at is not None,
            "last_played_at": last_played_at,
            "character_name": character.name if character is not None else None,
        }
    )


async def _load_turn_initial_state(
    session_id: int,
    message: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """Build the agent's initial state from DB rows. Raises 404 if the session is gone.

    Call only while holding the turn lock. Ends the read-only transaction before returning so
    the request's pooled connection is not held across the turn's LLM calls (the turn's writes
    use their own short-lived session in `persist_turn`).
    """
    session = await get_or_404(db, SessionModel, session_id, "Session")

    camp_result = await db.execute(select(Campaign).where(Campaign.id == session.campaign_id))
    campaign = camp_result.scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found for session")
    if campaign.status == "ended":
        raise HTTPException(
            status_code=409,
            detail="Campaign has ended; reopen it to continue.",
        )
    from ..services.campaign_readiness import evaluate_play_readiness, require_play_readiness

    sources = (
        []
        if campaign.source_mode == "homebrew"
        else await source_reads(list(campaign.adventure_collections or []))
    )
    readiness = await evaluate_play_readiness(
        db,
        campaign,
        character_id=session.active_character_id,
        require_character=True,
        session_id=session.id,
        observed_sources=sources,
    )
    require_play_readiness(readiness)
    from edgar_core.manifests import ManifestError

    from ..services.adventure_manifests import campaign_manifests

    try:
        manifests = (
            {}
            if campaign.source_mode == "homebrew"
            else await run_in_threadpool(
                campaign_manifests, list(campaign.adventure_collections or [])
            )
        )
    except ManifestError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "adventure_manifest_review_required",
                "message": "Review and publish the adventure before starting or continuing play.",
            },
        ) from exc
    story_state = await load_story_state(db, session_id, session.campaign_id)
    from ..services.prepared_amendments import (
        PreparedAmendmentConflict,
        load_session_prepared_amendments,
    )

    try:
        prepared_overlays, prepared_amendment_events = await load_session_prepared_amendments(
            db, session_id, session.campaign_id, manifests
        )
    except PreparedAmendmentConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "saved_version_conflict",
                "message": "This saved session refers to an adventure version that needs review before play can continue.",
            },
        ) from exc
    world_flags = session_story_flags(
        story_state, manifests, await load_world_flags(db, session.campaign_id)
    )

    memory_summary, prior_messages = await load_session_context(db, session_id)
    character = None
    if session.active_character_id:
        character = await character_play_state(db, session.active_character_id, session.campaign_id)
    npcs = await load_npcs(db, session.campaign_id, session_id)
    npc_summaries = model_context_npcs(npcs, story_state)

    initial_state: dict[str, Any] = {
        "player_input": message,
        "turn_id": uuid4().hex,
        "story_state": story_state,
        "resolved_combat_outcomes": await load_resolved_combat_outcomes(db, session_id),
        "turn_snapshot": {
            "current_scene_id": session.current_scene_id,
            "active_character_id": session.active_character_id,
            "character": character,
        },
        "session_id": session_id,
        "campaign_id": session.campaign_id,
        "messages": prior_messages,
        "adventure_collections": (
            [] if campaign.source_mode == "homebrew" else list(campaign.adventure_collections or [])
        ),
        "source_mode": campaign.source_mode,
        "rules_edition": campaign.rules_edition,
        "adventure_manifests": manifests,
        "prepared_campaign_overlays": prepared_overlays,
        "prepared_amendment_events": prepared_amendment_events,
        "source_status": [s.model_dump() for s in sources],
        "retrieval_degraded": any(s.status != "ready" for s in sources),
        "world_flags": world_flags,
        "current_scene_id": session.current_scene_id
        or (next(iter(manifests.values()))["opening_scene_id"] if manifests else None),
        "character": character,
        "npcs": npc_summaries,
        "combat_captives": await load_combat_captives(db, session_id),
        "memory_summary": memory_summary,
    }
    combat = await load_active_combat(db, session_id)
    if combat:
        initial_state["combat_state"] = combat
    await db.commit()
    return initial_state


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(
    campaign_id: int | None = None,
    db: AsyncSession = Depends(get_session),
):
    """List sessions. Optional ?campaign_id= filter."""
    q = select(SessionModel)
    if campaign_id is not None:
        q = q.where(SessionModel.campaign_id == campaign_id)
    result = await db.execute(q)
    sessions = result.scalars().all()
    return [await _session_read(s, db) for s in sessions]


@router.get("/sessions/{session_id}", response_model=SessionRead)
async def get_session_by_id(session_id: int, db: AsyncSession = Depends(get_session)):
    session = await get_or_404(db, SessionModel, session_id, "Session")
    return await _session_read(session, db)


@router.get("/sessions/{session_id}/combat")
async def get_session_combat(session_id: int, db: AsyncSession = Depends(get_session)):
    """Active encounter in the same public shape as the stream's `done.combat_state`, or null.

    Lets the client restore the combat HUD after a reload.
    """
    await get_or_404(db, SessionModel, session_id, "Session")
    return combat_state_public(await load_active_combat(db, session_id))


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageRead])
async def list_session_messages(session_id: int, db: AsyncSession = Depends(get_session)):
    """Hydrate chat UI from narration rows in event_log (oldest first)."""
    await get_or_404(db, SessionModel, session_id, "Session")

    ev_result = await db.execute(
        select(EventLog)
        .where(EventLog.session_id == session_id, EventLog.event_type == "narration")
        .order_by(EventLog.created_at.asc())
    )
    events = ev_result.scalars().all()
    out: list[ChatMessageRead] = []
    for e in events:
        p = e.payload or {}
        if p.get("player_input"):
            out.append(
                ChatMessageRead(
                    role="user",
                    content=str(p["player_input"]),
                    event_id=e.id,
                    created_at=e.created_at,
                )
            )
        if p.get("narration"):
            # The stored original ruling and dice; reload never re-rolls (EDG-030).
            roll_status, roll = stored_roll(p)
            out.append(
                ChatMessageRead(
                    role="dm",
                    content=str(p["narration"]),
                    event_id=e.id,
                    citations=public_citations(p.get("citations", [])),
                    rules_edition=p.get("rules_edition"),
                    ruling_citation_ids=[
                        rid
                        for rid in p.get("ruling_citation_ids", [])
                        if rid
                        in {r["reference_id"] for r in public_citations(p.get("citations", []))}
                    ],
                    turn_id=p.get("turn_id"),
                    roll_status=roll_status,
                    roll=roll,
                    created_at=e.created_at,
                )
            )
    return out


@router.get("/sessions/{session_id}/diagnostics/combat")
async def list_combat_diagnostics(session_id: int, db: AsyncSession = Depends(get_session)):
    """Stored combat-round diagnostics for reload, oldest first (developer only, EDG-031).

    Only rounds that were written with this feature appear. A turn with no entry has no stored
    combat telemetry, which the UI must show as not captured, never as zero or inferred.
    """
    if not debug_turns_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    await get_or_404(db, SessionModel, session_id, "Session")
    rows = (
        (
            await db.execute(
                select(EventLog)
                .where(EventLog.session_id == session_id, EventLog.event_type == COMBAT_ROUND_EVENT)
                .order_by(EventLog.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {"created_at": row.created_at, **(combat_round_public(row.payload) or {})} for row in rows
    ]


@router.post("/sessions/{session_id}/intro")
async def session_intro(
    session_id: int,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Generate the opening scene for a fresh session (idempotent).

    The UI calls this when a session has no history yet, so the player lands on a DM-set scene
    instead of a blank chat. Returns `{started: true, narration, character, current_scene_id}`
    when it created the opening, or `{started: false}` if the session already has narration.
    """
    # Reuse the per-session turn lock so an intro can't race a first turn (or another intro).
    # Taken before reading the session so the intro acts on current state.
    lock = await acquire_turn_lock(session_id, redis)
    if lock is None:
        raise HTTPException(status_code=409, detail="Session is busy; please retry.")
    try:
        session = await get_or_404(db, SessionModel, session_id, "Session")

        camp_res = await db.execute(select(Campaign).where(Campaign.id == session.campaign_id))
        campaign = camp_res.scalar_one_or_none()
        if campaign is not None and campaign.status == "ended":
            raise HTTPException(
                status_code=409, detail="Campaign has ended; reopen it to continue."
            )

        existing_intro = await db.execute(
            select(EventLog.id)
            .where(EventLog.session_id == session_id, EventLog.event_type == "narration")
            .limit(1)
        )
        if existing_intro.scalar_one_or_none() is not None:
            return {"started": False}

        payload = await generate_session_intro(db, session)
        if payload is not None:
            await db.commit()
    finally:
        await lock.release()

    if payload is None:
        return {"started": False}
    return {"started": True, **payload}


@router.post("/sessions", response_model=SessionRead)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_session)):
    campaign = await db.get(Campaign, body.campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    sources = (
        []
        if campaign.source_mode == "homebrew"
        else await source_reads(list(campaign.adventure_collections or []))
    )
    from ..services.campaign_readiness import evaluate_play_readiness, require_play_readiness

    readiness = await evaluate_play_readiness(
        db,
        campaign,
        character_id=body.active_character_id,
        # Generic campaigns allow a no-character session; prepared campaigns still enforce
        # their commissioned hero when this is a session-start preflight.
        require_character=True,
        observed_sources=sources,
    )
    require_play_readiness(readiness)
    started_at = body.started_at if body.started_at is not None else datetime.now(UTC)
    session = SessionModel(
        campaign_id=body.campaign_id,
        started_at=started_at,
        ended_at=body.ended_at,
        active_character_id=body.active_character_id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return await _session_read(session, db)


@router.patch("/sessions/{session_id}", response_model=SessionRead)
async def update_session(
    session_id: int, body: SessionUpdate, db: AsyncSession = Depends(get_session)
):
    session = await get_or_404(db, SessionModel, session_id, "Session")
    update_data = body.model_dump(exclude_unset=True)
    # Same invariant create enforces: the active character must be playing in this campaign.
    new_character_id = update_data.get("active_character_id")
    campaign = await get_or_404(db, Campaign, session.campaign_id, "Campaign")
    from ..services.prepared_play import require_prepared_play

    await run_in_threadpool(
        require_prepared_play,
        [] if campaign.source_mode == "homebrew" else campaign.adventure_collections,
        campaign.rules_edition,
        character_id=update_data.get("active_character_id", session.active_character_id),
        require_hero=True,
    )
    if new_character_id is not None:
        ass = await load_active_assignment(db, new_character_id, session.campaign_id)
        if ass is None:
            raise HTTPException(
                status_code=422,
                detail="Character has no active assignment in this campaign",
            )
    for key, value in update_data.items():
        setattr(session, key, value)
    await db.commit()
    await db.refresh(session)
    return await _session_read(session, db)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: int, db: AsyncSession = Depends(get_session)):
    session = await get_or_404(db, SessionModel, session_id, "Session")
    await db.delete(session)
    await db.commit()


def _turn_error_response(err: TurnError) -> JSONResponse:
    return JSONResponse(status_code=err.status_code, content=err.body())


def _turn_http_error_response(exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = detail.get("code")
    if exc.detail == _BUSY_DETAIL:
        code = "session_busy"
    elif exc.detail == "Campaign has ended; reopen it to continue.":
        code = "campaign_ended"
    failure = classify_http_failure(exc.status_code, code=code, outcome="not_committed")
    if failure.category == "busy":
        failure = classify_http_failure(exc.status_code, code=code, outcome="unknown")
    return JSONResponse(status_code=failure.status_code, content=failure.to_payload())


async def _replayed_turn(
    db: AsyncSession, session_id: int, body: TurnRequest
) -> TurnResponse | None:
    if body.request_id is None:
        return None
    await get_or_404(db, SessionModel, session_id, "Session")
    receipt = await load_turn_receipt(db, session_id, body.request_id)
    if receipt is None:
        return None
    original_input, result = receipt
    if original_input != body.message:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "turn_identity_conflict",
                "message": "This action identity was already used.",
            },
        )
    return result


@router.get("/sessions/{session_id}/turns/{request_id}")
async def get_turn_receipt(
    session_id: int,
    response: Response,
    request_id: str = Path(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Resolve a lost acknowledgment without generating or applying an action.

    Lock contention means the original turn may still commit: absence must never be
    reported as final while that writer is active. Clients retry only by explicit action.
    """
    response.headers["Cache-Control"] = "private, no-store"
    lock = await acquire_turn_lock(session_id, redis)
    if lock is None:
        raise HTTPException(status_code=409, detail=classify_error("session_busy").to_payload())
    try:
        await get_or_404(db, SessionModel, session_id, "Session")
        receipt = await load_turn_receipt(db, session_id, request_id)
        if receipt is None:
            raise HTTPException(
                status_code=404,
                detail=classify_error("turn_not_found", outcome="not_committed").to_payload(),
            )
        return {"status": "committed", "result": receipt[1]}
    finally:
        await lock.release()


@router.post("/sessions/{session_id}/turn", response_model=TurnResponse)
async def session_turn(
    session_id: int,
    body: TurnRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Run one player turn synchronously and return the full result.

    Lifecycle: acquire Redis lock (409 on contention) -> load state -> run agent -> persist
    the whole turn in one transaction -> release lock in `finally` -> update the rolling memory
    summary in the background. Failures return `{"code", "message", "detail"}` with a generic
    message (409 missing assignment, 503 agent/LLM failure, 500 otherwise); details are logged.
    """
    lock = await acquire_turn_lock(session_id, redis)
    if lock is None:
        return _turn_http_error_response(HTTPException(status_code=409, detail=_BUSY_DETAIL))

    structlog.contextvars.bind_contextvars(session_id=session_id)
    outcome = "not_committed"
    try:
        replay = await _replayed_turn(db, session_id, body)
        if replay is not None:
            return replay
        initial_state = await _load_turn_initial_state(session_id, body.message, db)
        if body.request_id is not None:
            initial_state["turn_id"] = body.request_id
        structlog.contextvars.bind_contextvars(campaign_id=initial_state["campaign_id"])

        result = {**initial_state, **await run_turn(initial_state)}
        outcome = "unknown"
        done = await persist_turn(session_id, body.message, result)
        outcome = "committed"
        background_tasks.add_task(maybe_update_memory_summary, session_id)

        adjudication = result.get("adjudication_result")
        return TurnResponse(
            event_id=done.get("event_id"),
            narration=done["narration"],
            retrieval_degraded=done["retrieval_degraded"],
            source_status=done["source_status"],
            source_mode=done["source_mode"],
            adjudication=public_adjudication(adjudication),
            citations=done["citations"],
            rules_edition=done["rules_edition"],
            ruling_citation_ids=done["ruling_citation_ids"],
            # The public roster shape only: raw combatants carry planner-only creature provenance.
            combat_state=done["combat_state"],
            current_scene_id=done["current_scene_id"],
            character=done["character"],
            campaign_complete=done["campaign_complete"],
            combat_diagnostics=(
                combat_round_from_events(result.get("telemetry_events")) if body.debug else None
            ),
            roll_status=done.get("roll_status"),
            roll=done.get("roll"),
        )
    except HTTPException as exc:
        return _turn_http_error_response(exc)
    except TurnError as e:
        return _turn_error_response(e)
    except Exception as exc:
        failure = TurnError(exc, outcome=outcome)
        log_failure("turn_failed", failure.failure, session_id=session_id)
        return _turn_error_response(failure)
    finally:
        structlog.contextvars.unbind_contextvars("session_id", "campaign_id")
        await lock.release()


@router.post("/sessions/{session_id}/turn/stream")
async def session_turn_stream(
    session_id: int,
    body: TurnRequest,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Stream the turn as Server-Sent Events; the lock is held for the lifetime of the stream.

    A 409 (lock contention) or 404 is returned as a regular JSON error before the stream opens.
    Errors after the stream begins are emitted as `error` SSE frames; the connection then
    closes and the lock is released in the generator's `finally`. A client that disconnects
    before the body starts never runs that `finally`, so the background task releases it too.
    """
    lock = await acquire_turn_lock(session_id, redis)
    if lock is None:
        return _turn_http_error_response(HTTPException(status_code=409, detail=_BUSY_DETAIL))
    try:
        replay = await _replayed_turn(db, session_id, body)
        if replay is not None:
            await db.commit()
            await lock.release()
            return StreamingResponse(
                iter([sse_event("done", replay.model_dump())]),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        initial_state = await _load_turn_initial_state(session_id, body.message, db)
        if body.request_id is not None:
            initial_state["turn_id"] = body.request_id
    except HTTPException as exc:
        await lock.release()
        return _turn_http_error_response(exc)
    except BaseException:
        await lock.release()
        raise

    async def event_gen():
        structlog.contextvars.bind_contextvars(
            session_id=session_id,
            campaign_id=initial_state["campaign_id"],
        )
        try:
            async for chunk in stream_session_turn(
                session_id=session_id,
                body_message=body.message,
                initial_state=initial_state,
                debug=body.debug,
            ):
                yield chunk
        finally:
            structlog.contextvars.unbind_contextvars("session_id", "campaign_id")
            await lock.release()

    background = BackgroundTasks()
    # Idempotent, so it is a no-op after the generator's `finally` already released the lock.
    background.add_task(lock.release)
    # Runs after the stream closes (and the lock is released); never fails the turn.
    background.add_task(maybe_update_memory_summary, session_id)
    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
        background=background,
    )
