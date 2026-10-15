"""Load LangChain messages from event_log for turn continuity, plus the rolling memory summary.

The narrator sees the last RECENT_TURNS turns verbatim. Turns older than that are folded into a
persisted "story so far" summary (an event_log row with event_type=MEMORY_SUMMARY_EVENT whose
payload records which narration row it covers up to), updated off the critical path after a
turn commits and only once SUMMARY_EVERY_N_TURNS new turns have aged out of the window.
"""

from dataclasses import dataclass
from typing import Literal

import structlog
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import BigInteger, and_, case, cast, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

import db.postgres.session as db_session_module
from agent.nodes.memory_summarizer import summarize_story
from db.postgres.models import EventLog, Session as SessionModel

log = structlog.get_logger()

RECENT_TURNS = 10
SUMMARY_EVERY_N_TURNS = 5
MEMORY_SUMMARY_EVENT = "memory_summary"
MAX_GENERATION_ATTEMPTS = 3


def _events_to_messages(events: list[EventLog]) -> list[HumanMessage | AIMessage]:
    messages: list[HumanMessage | AIMessage] = []
    for e in events:
        payload = e.payload or {}
        player_input = payload.get("player_input")
        narration = payload.get("narration")
        if player_input:
            messages.append(HumanMessage(content=str(player_input)))
        if narration:
            messages.append(AIMessage(content=str(narration)))
    return messages


def _summary_coverage_query(session_id: int, summary: EventLog):
    """Select each nonempty summary with the verified narration id it actually covers."""
    narration = aliased(EventLog)
    raw_through = summary.payload["through_event_id"].astext
    parsed_through = case(
        (raw_through.op("~")("^[0-9]{1,18}$"), cast(raw_through, BigInteger)),
        else_=0,
    )
    coverage = func.coalesce(narration.id, 0)
    query = (
        select(summary, coverage)
        .outerjoin(
            narration,
            (narration.id == parsed_through)
            & (narration.session_id == summary.session_id)
            & (narration.event_type == "narration")
            & (narration.id < summary.id),
        )
        .where(
            summary.session_id == session_id,
            summary.event_type == MEMORY_SUMMARY_EVENT,
            func.jsonb_typeof(summary.payload["summary"]) == "string",
            summary.payload["summary"].astext.op("~")("[^[:space:]]"),
        )
    )
    return query, coverage


async def load_session_messages(
    db: AsyncSession,
    session_id: int,
    *,
    limit_turns: int = RECENT_TURNS,
) -> list[HumanMessage | AIMessage]:
    """Load last N narration turns (player_input + narration pairs) from event_log, oldest first."""
    result = await db.execute(
        select(EventLog)
        .where(
            EventLog.session_id == session_id,
            EventLog.event_type == "narration",
        )
        .order_by(EventLog.created_at.desc(), EventLog.id.desc())
        .limit(limit_turns)
    )
    return _events_to_messages(list(reversed(result.scalars().all())))


async def load_session_context(
    db: AsyncSession, session_id: int
) -> tuple[str | None, list[HumanMessage | AIMessage]]:
    """Load the summary and every narration event it does not cover in one statement.

    Prompt history must start immediately after the selected summary's validated boundary.
    A fixed recent-turn limit would silently drop events when generation is delayed or fails.
    The single statement also prevents a summary publication between separate summary/history
    reads from producing a context with a gap.
    """
    summary = aliased(EventLog)
    summary_query, coverage = _summary_coverage_query(session_id, summary)
    latest_summary = (
        summary_query.with_only_columns(
            summary.id.label("id"),
            summary.payload["summary"].astext.label("summary"),
            coverage.label("through_event_id"),
        )
        .order_by(coverage.desc(), summary.id.desc())
        .limit(1)
        .subquery()
    )
    rows = await db.execute(
        select(
            latest_summary.c.summary,
            latest_summary.c.through_event_id,
            EventLog,
        )
        .select_from(SessionModel)
        .outerjoin(latest_summary, true())
        .outerjoin(
            EventLog,
            and_(
                EventLog.session_id == SessionModel.id,
                EventLog.event_type == "narration",
                EventLog.id > func.coalesce(latest_summary.c.through_event_id, 0),
            ),
        )
        .where(SessionModel.id == session_id)
        .order_by(EventLog.id.asc())
    )
    result = rows.all()
    if not result:
        return None, []
    summary_text = result[0][0]
    events = [row[2] for row in result if row[2] is not None]
    return summary_text, _events_to_messages(events)


async def _latest_summary_row(db: AsyncSession, session_id: int) -> tuple[EventLog, int] | None:
    """Choose maximum *valid* narration coverage, with insertion order only breaking ties.

    Legacy rows remain untouched. Missing/malformed boundaries can supply fallback text,
    but cannot claim coverage or seed incremental generation. Validate the pointer against
    this session's narration, so unrelated event ids cannot hide unsummarized history.
    """
    summary_query, coverage = _summary_coverage_query(session_id, EventLog)
    result = await db.execute(
        summary_query
        .order_by(coverage.desc(), EventLog.id.desc())
        .limit(1)
    )
    row = result.one_or_none()
    return (row[0], row[1]) if row is not None else None


async def load_memory_summary(db: AsyncSession, session_id: int) -> str | None:
    """The persisted story with greatest valid narration coverage, or legacy fallback text."""
    latest = await _latest_summary_row(db, session_id)
    return latest[0].payload["summary"] if latest is not None else None


@dataclass(frozen=True)
class _MemorySnapshot:
    base_summary_id: int | None
    previous_summary: str | None
    through_event_id: int
    turns_summarized: int
    # Only immutable primitives leave the transaction; no attached ORM/message objects.
    messages: tuple[tuple[str, str], ...]


async def _read_memory_snapshot(session_id: int, *, min_turns: int) -> _MemorySnapshot | None:
    async with db_session_module.async_session_factory() as db, db.begin():
        # Match turn publication's Session-first lock order. Under READ COMMITTED this
        # stabilizes summary publication and committed turn writes across the two reads.
        owner = await db.scalar(
            select(SessionModel.id).where(SessionModel.id == session_id).with_for_update()
        )
        if owner is None:
            return None
        latest = await _latest_summary_row(db, session_id)
        through_id = latest[1] if latest is not None else 0
        result = await db.execute(
            select(EventLog)
            .where(
                EventLog.session_id == session_id,
                EventLog.event_type == "narration",
                EventLog.id > through_id,
            )
            .order_by(EventLog.id.asc())
        )
        pending = list(result.scalars().all())
        aged_out = pending[:-RECENT_TURNS] if len(pending) > RECENT_TURNS else []
        if len(aged_out) < min_turns:
            return None
        return _MemorySnapshot(
            base_summary_id=latest[0].id if latest is not None else None,
            previous_summary=latest[0].payload["summary"] if through_id else None,
            through_event_id=aged_out[-1].id,
            turns_summarized=len(aged_out),
            messages=tuple((m.type, str(m.content)) for m in _events_to_messages(aged_out)),
        )


async def _publish_memory_summary(
    session_id: int, snapshot: _MemorySnapshot, summary: str
) -> Literal["published", "obsolete", "rebase"]:
    async with db_session_module.async_session_factory() as db, db.begin():
        owner = await db.scalar(
            select(SessionModel.id).where(SessionModel.id == session_id).with_for_update()
        )
        if owner is None:
            return "obsolete"
        latest = await _latest_summary_row(db, session_id)
        if latest is not None and latest[1] >= snapshot.through_event_id:
            return "obsolete"
        if (latest[0].id if latest is not None else None) != snapshot.base_summary_id:
            # A result must describe exactly its recorded incremental base. Regenerate
            # against the winner instead of combining text generated from different bases.
            return "rebase"
        db.add(
            EventLog(
                session_id=session_id,
                event_type=MEMORY_SUMMARY_EVENT,
                payload={
                    "summary": summary,
                    "through_event_id": snapshot.through_event_id,
                    "turns_summarized": snapshot.turns_summarized,
                    "base_summary_event_id": snapshot.base_summary_id,
                },
            )
        )
    return "published"


async def maybe_update_memory_summary(session_id: int) -> None:
    """Generate outside DB transactions and publish only against the current summary base.

    Overlapping equal/older jobs are no-ops. A changed base retries from fresh immutable
    input, at most MAX_GENERATION_ATTEMPTS times to bound provider work under contention.
    Exhausted/failed jobs leave history intact; later background jobs retry aged-out turns.
    Cancellation propagates while context managers release connections/roll back writes.
    """
    try:
        for attempt in range(MAX_GENERATION_ATTEMPTS):
            snapshot = await _read_memory_snapshot(
                session_id,
                # Once a job qualified, finish even a smaller remainder after rebasing.
                min_turns=SUMMARY_EVERY_N_TURNS if attempt == 0 else 1,
            )
            if snapshot is None:
                return
            messages = [
                HumanMessage(content=text) if kind == "human" else AIMessage(content=text)
                for kind, text in snapshot.messages
            ]
            summary = await summarize_story(snapshot.previous_summary, messages)
            if not summary or not summary.strip():
                return
            outcome = await _publish_memory_summary(session_id, snapshot, summary)
            if outcome == "rebase":
                continue
            if outcome == "published":
                log.info(
                    "memory_summary_updated", session_id=session_id, turns=snapshot.turns_summarized
                )
            return
        log.info("memory_summary_contended", session_id=session_id)
    except Exception:
        log.warning("memory_summary_failed", session_id=session_id, exc_info=True)
