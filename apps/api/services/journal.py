"""Project the session-owned committed ledger into a small player-safe journal.

This module deliberately consumes no adventure manifest or prepared template. A row is
eligible only when the shared story projection validates a matching player-audience source
origin. Every response field is then constructed from a fixed allowlist.
"""

from __future__ import annotations

import base64
import hashlib
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from db.postgres.models import EventLog
from edgar_core.story import StoryFact, player_story_effect, story_target_id

from ..schemas.journal import JournalEntry, JournalOrigin, JournalRead, JournalStatusChange

_CATEGORY = {
    "discover": "clue",
    "encounter": "person",
    "relationship": "person",
    "visit": "place",
    "move": "place",
    "objective": "goal",
    "milestone": "milestone",
    "promise": "promise",
    "clock": "clock",
    "environment": "consequence",
}
_TITLE = {
    "goal": "Goal",
    "milestone": "Milestone",
    "clue": "Clue",
    "hint": "Hint",
    "person": "Person",
    "place": "Known place",
    "promise": "Promise",
    "clock": "Story urgency",
    "consequence": "Story change",
}
_ALLOWED_STATUS = {
    "discovered",
    "encountered",
    "visited",
    "active",
    "completed",
    "failed",
    "fulfilled",
    "broken",
    "advanced",
    "friendly",
    "neutral",
    "hostile",
    "changed",
}


def _entry_id(session_id: int, category: str, stable_fact_id: str) -> str:
    """Return a stable, session-scoped link handle without exposing a canonical identity."""
    raw = f"edgar-journal-v1:{session_id}:{category}:{stable_fact_id}".encode()
    digest = base64.urlsafe_b64encode(hashlib.blake2s(raw, digest_size=16).digest())
    return "entry_" + digest.decode("ascii").rstrip("=")


def _fact_event(fact: StoryFact, event_ids: set[int]) -> int | None:
    return fact.event_id if fact.event_id in event_ids else None


def _citation(public_fact: dict[str, Any]) -> str | None:
    """Expose only a player-safe page citation, never source IDs, hashes, or collection paths."""
    origins = public_fact.get("origins") or []
    if not origins:
        return None
    origin = origins[0]
    page = origin.get("page")
    if type(page) is not int or page < 1:
        return None
    page_end = origin.get("page_end")
    if type(page_end) is int and page_end > page:
        return f"Pages {page}–{page_end}"
    return f"Page {page}"


def _safe_status(fact: StoryFact) -> str | None:
    # Clock completion is currently inferred from its private clock_max by the validator.
    # Until the ledger records an explicit player-reveal marker, only qualitative urgency is
    # safe to expose; terminal status and count of updates can reveal a hidden threshold.
    if fact.kind == "clock":
        return "advanced"
    return fact.status if fact.status in _ALLOWED_STATUS else None


def _group_key(fact: StoryFact, category: str) -> tuple[str, str]:
    # Each committed environment change is its own earned event. Other mutable records share
    # one canonical player-facing entry whose history retains its status transitions.
    identity = fact.effect_id if fact.kind == "environment" else fact.fact_id
    return category, identity


def project_player_journal(
    session_id: int,
    state: dict[str, Any],
    narration_events: Iterable[EventLog],
) -> JournalRead:
    """Build the complete safe projection for one selected session.

    `narration_events` must already be constrained to the session. The extra ownership check
    here ensures history links are emitted only for rows that are actually that session's
    narration events. Unknown/private facts never influence the output list or its counts.
    """
    events = sorted(
        (
            event
            for event in narration_events
            if event.session_id == session_id and event.event_type == "narration"
        ),
        key=lambda event: (event.created_at, event.id),
    )
    event_order = {event.id: index + 1 for index, event in enumerate(events)}
    turn_events: dict[str, int] = {}
    for event in events:
        turn_id = (event.payload or {}).get("turn_id")
        if isinstance(turn_id, str):
            turn_events.setdefault(turn_id, event.id)

    facts = [StoryFact.model_validate(raw) for raw in state.get("facts", [])]
    facts.sort(key=lambda fact: (event_order.get(fact.event_id, 0), fact.event_id or 0))
    safe_records: list[tuple[StoryFact, dict[str, Any], str, int | None]] = []
    for fact in facts:
        category = _CATEGORY.get(fact.kind)
        if category is None:
            continue
        public_fact = player_story_effect(fact)
        status = _safe_status(fact)
        if public_fact is None or status is None:
            continue
        # Narrow the shared projection to fields needed by this journal implementation.
        safe_records.append(
            (
                fact,
                {"name": public_fact["name"], "text": public_fact["text"], "origins": public_fact["origins"]},
                category,
                _fact_event(fact, set(event_order)),
            )
        )

    # The latest committed, player-named location is the only location we can call current.
    # Its exact text remains attached as an entry and is never reconstructed from prose.
    place_at_event: dict[int, str | None] = {}
    latest_place: str | None = None
    for _, public_fact, category, event_id in safe_records:
        if category == "place":
            latest_place = public_fact["name"] or None
        if event_id is not None:
            # If one narration commits more than one location-related fact, origins from
            # that turn resolve to the location after all of its committed facts.
            place_at_event[event_id] = latest_place

    grouped: dict[tuple[str, str], list[tuple[StoryFact, dict[str, Any], int | None]]] = defaultdict(list)
    for fact, public_fact, category, event_id in safe_records:
        grouped[_group_key(fact, category)].append((fact, public_fact, event_id))

    # Resolve promise actors only to a person already present in this same earned projection.
    known_people: dict[str, tuple[str, str]] = {}
    for (category, stable_id), records in grouped.items():
        if category != "person":
            continue
        names = [record[1]["name"] for record in records if record[1]["name"]]
        if names:
            known_people[stable_id] = (_entry_id(session_id, category, stable_id), names[0])

    built: list[tuple[int, JournalEntry]] = []
    for (category, stable_id), records in grouped.items():
        records.sort(key=lambda record: (event_order.get(record[2], 0), record[0].event_id or 0))
        first_fact, first_public, first_event_id = records[0]
        latest_fact, _, _ = records[-1]
        safe_names = [record[1]["name"] for record in records if record[1]["name"]]
        title = safe_names[0] if safe_names else _TITLE[category]
        origin_event_id = first_event_id
        if first_fact.origin_turn_id:
            origin_event_id = turn_events.get(first_fact.origin_turn_id, origin_event_id)
        origin_turn = event_order.get(origin_event_id) if origin_event_id is not None else None
        origin_place = place_at_event.get(origin_event_id) if origin_event_id is not None else None
        actor: str | None = None
        related_entry_id: str | None = None
        if category == "promise" and first_fact.promise_actor is not None:
            actor_record = known_people.get(story_target_id(first_fact.promise_actor))
            if actor_record is not None:
                related_entry_id, actor = actor_record

        history: list[JournalStatusChange] = []
        if category == "clock":
            # The old ledger has no explicit reveal bit, and validator-generated `completed`
            # may encode a private maximum. Keep one qualitative state with no progress count.
            history = [
                JournalStatusChange(
                    status="advanced",
                    turn_number=origin_turn,
                    history_event_id=origin_event_id,
                )
            ]
        else:
            for fact, _, accessible_event_id in records:
                status = _safe_status(fact)
                if status is None:
                    continue
                turn_number = event_order.get(accessible_event_id) if accessible_event_id is not None else None
                item = JournalStatusChange(
                    status=status,
                    turn_number=turn_number,
                    history_event_id=accessible_event_id,
                )
                if not history or history[-1] != item:
                    history.append(item)
        current_status = _safe_status(latest_fact)
        if current_status is None:
            continue
        citation = _citation(first_public)
        entry = JournalEntry(
            id=_entry_id(session_id, category, stable_id),
            category=category,  # type: ignore[arg-type]
            title=title,
            # Preserve the first exact earned text as the immutable original. Status changes
            # remain separate in history, so later clues or summaries cannot replace it.
            text=first_public["text"],
            status=current_status,  # type: ignore[arg-type]
            # Candidate counts are withheld until the ledger gets an explicit player reveal.
            quantity=None,
            origin=JournalOrigin(
                turn_number=origin_turn,
                history_event_id=origin_event_id,
                place=origin_place,
                actor=actor,
                citation=citation,
            ),
            history=history,
            related_entry_id=related_entry_id,
        )
        latest_sequence = max(
            (event_order.get(event_id, 0) for _, _, event_id in records),
            default=0,
        )
        built.append((latest_sequence, entry))

    built.sort(key=lambda item: item[0], reverse=True)
    entries = [entry for _, entry in built]
    current_place = next((entry for _, entry in built if entry.category == "place"), None)
    active_goal = next(
        (entry for _, entry in built if entry.category == "goal" and entry.status == "active"),
        None,
    )
    tracking = "available" if entries or not events else "legacy_untracked"
    return JournalRead(
        session_id=session_id,
        tracking=tracking,
        entries=entries,
        current_place=current_place,
        active_goal=active_goal,
    )


def filter_player_journal(
    journal: JournalRead,
    *,
    category: str | None = None,
    query: str | None = None,
) -> JournalRead:
    """Filter only an already safe projection; hidden state cannot affect result counts."""
    entries = journal.entries
    if category:
        entries = [entry for entry in entries if entry.category == category]
    normalized_query = (query or "").strip().casefold()
    if normalized_query:
        entries = [
            entry
            for entry in entries
            if normalized_query
            in " ".join(
                filter(
                    None,
                    (
                        entry.title,
                        entry.text,
                        entry.status,
                        entry.origin.place,
                        entry.origin.actor,
                        entry.origin.citation,
                    ),
                )
            ).casefold()
        ]
    return journal.model_copy(update={"entries": entries})
