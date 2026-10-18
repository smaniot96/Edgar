"""Project the session-owned committed ledger into a small player-safe journal.

This module deliberately consumes no adventure manifest or prepared template. A row is
eligible only when the shared story projection validates a matching player-audience source
origin. Every response field is then constructed from a fixed allowlist.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from db.postgres.models import EventLog
from edgar_core.story import StoryFact, player_story_effect, story_target_id

from ..schemas.journal import JournalEntry, JournalOrigin, JournalRead, JournalStatusChange
from .journal_identity import (
    actor_entry_id,
    actor_identity_id,
    earned_actor_label,
    journal_entry_id,
)

_CATEGORY = {
    "discover": "clue",
    "encounter": "person",
    "relationship": "person",
    "visit": "place",
    "move": "place",
    "learn_place": "place",
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
    "known",
    "announced",
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
    "captive",
    "released",
    "dead",
    "defeated",
}


# Narration rows written by journal-aware code carry this version. A session with no entries
# is legacy only when its first narration row lacks it: a new session can have none yet.
JOURNAL_TRACKING_VERSION = 1


def journal_tracking_marker() -> dict[str, int]:
    """Payload fields that stamp a narration row as written under journal tracking."""
    return {"journal_tracking_version": JOURNAL_TRACKING_VERSION}


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
    if fact.kind == "clock":
        disclosure = getattr(fact, "clock_disclosure", None)
        if disclosure is None or not disclosure.completion:
            return "advanced"
    return fact.status if fact.status in _ALLOWED_STATUS else None


def _group_key(fact: StoryFact, category: str, session_id: int) -> tuple[str, str]:
    # Actor references retain one session identity across source/manifest revisions. Their
    # individual facts still pass source validation above before they enter this projection.
    if category == "person" and fact.target.kind == "entity":
        return category, actor_identity_id(session_id, fact.target)
    # Each committed environment change is its own earned event. Other mutable records share
    # one canonical player-facing entry whose history retains its status transitions.
    identity = fact.effect_id if fact.kind == "environment" else fact.fact_id
    return category, identity


def stable_title_context(
    records: list[tuple[StoryFact, dict[str, Any], int | None]],
    event_order: dict[int, int],
) -> str:
    first_event = next((record[2] for record in records if record[2] is not None), None)
    turn = event_order.get(first_event) if first_event is not None else None
    return f"Person encountered on turn {turn}" if turn is not None else "Encountered person"


def project_player_journal(
    session_id: int,
    state: dict[str, Any],
    narration_events: Iterable[EventLog],
    *,
    active_character_id: int | None = None,
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
    object_ownership: dict[str, str] = {}
    for fact in facts:
        public_fact = player_story_effect(fact)
        if public_fact is None:
            continue
        if fact.kind in {"acquire", "consume", "transfer"} and fact.target.kind == "object":
            identity = story_target_id(fact.target)
            if fact.kind == "acquire":
                destination = fact.destination or ""
                if active_character_id is None:
                    object_ownership[identity] = "untracked"
                elif destination == f"character:{active_character_id}":
                    object_ownership[identity] = "carried"
                elif destination.startswith("character:"):
                    object_ownership[identity] = "with_another_character"
                else:
                    object_ownership[identity] = "untracked"
            elif fact.kind == "transfer":
                object_ownership[identity] = "left_behind"
            else:
                object_ownership[identity] = "consumed"
            continue
        category = _CATEGORY.get(fact.kind)
        if fact.kind == "discover" and getattr(fact, "knowledge_kind", None) == "hint":
            category = "hint"
        if category is None:
            continue
        status = _safe_status(fact)
        if public_fact is None or status is None:
            continue
        # Narrow the shared projection to fields needed by this journal implementation.
        safe_records.append(
            (
                fact,
                {
                    "name": (
                        earned_actor_label(fact.display_name, public_fact["text"])
                        if fact.kind in {"encounter", "relationship"}
                        else public_fact["name"]
                    ),
                    "text": public_fact["text"],
                    "origins": public_fact["origins"],
                    "quantity": public_fact.get("quantity"),
                },
                category,
                _fact_event(fact, set(event_order)),
            )
        )

    # The latest committed, player-named location is the only location we can call current.
    # Its exact text remains attached as an entry and is never reconstructed from prose.
    place_at_event: dict[int, tuple[str | None, str | None]] = {}
    latest_place: str | None = None
    latest_place_entry_id: str | None = None
    for fact, public_fact, category, event_id in safe_records:
        if category == "place" and fact.kind in {"visit", "move"}:
            latest_place = public_fact["name"] or None
            latest_place_entry_id = journal_entry_id(session_id, "place", fact.fact_id)
        if event_id is not None:
            # If one narration commits more than one location-related fact, origins from
            # that turn resolve to the location after all of its committed facts.
            place_at_event[event_id] = (latest_place, latest_place_entry_id)

    grouped: dict[tuple[str, str], list[tuple[StoryFact, dict[str, Any], int | None]]] = defaultdict(list)
    # The immutable ledger order also resolves multiple consequences in one narration.
    # For example, an initial visit followed by a move must orient to the destination.
    fact_order = {fact.effect_id: index for index, (fact, _, _, _) in enumerate(safe_records)}
    for fact, public_fact, category, event_id in safe_records:
        grouped[_group_key(fact, category, session_id)].append((fact, public_fact, event_id))

    # Resolve promise actors only to a person already present in this same earned projection.
    # These event links power adjacent, immutable narration references in the player UI.
    known_people: dict[str, tuple[str, str, str]] = {}
    actor_reference_events: dict[str, set[int]] = defaultdict(set)
    known_object_clues: dict[str, str] = {}
    for (category, stable_id), records in grouped.items():
        first_fact = records[0][0]
        if category in {"clue", "hint"}:
            related_object = getattr(first_fact, "related_object", None)
            if related_object is not None:
                known_object_clues[story_target_id(related_object)] = journal_entry_id(
                    session_id, category, stable_id
                )
        if category != "person":
            continue
        identity = actor_entry_id(session_id, first_fact.target)
        context = stable_title_context(records, event_order)
        latest_named = next(
            ((record[0], record[1]["name"]) for record in reversed(records) if record[1]["name"]),
            None,
        )
        if latest_named is None:
            safe_title = context
        else:
            named_fact, earned_name = latest_named
            safe_title = earned_name
            if earned_name.casefold() != named_fact.display_name.casefold():
                safe_title = f"{earned_name} · {context.casefold()}"
        known_people[stable_id] = (identity, safe_title, context)
        actor_reference_events[stable_id].update(
            event_id for _, _, event_id in records if event_id is not None
        )

    for (category, _), records in grouped.items():
        if category != "promise":
            continue
        promise_actor = records[0][0].promise_actor
        if promise_actor is None:
            continue
        actor_key = actor_identity_id(session_id, promise_actor)
        actor_reference_events[actor_key].update(
            event_id for _, _, event_id in records if event_id is not None
        )

    duplicate_labels: dict[str, list[str]] = defaultdict(list)
    for actor_key, (_, label, _) in known_people.items():
        duplicate_labels[label.casefold()].append(actor_key)
    for actor_keys in duplicate_labels.values():
        if len(actor_keys) < 2:
            continue
        contexts = [known_people[actor_key][2].casefold() for actor_key in actor_keys]
        repeated_context = len(set(contexts)) != len(contexts)
        for index, actor_key in enumerate(actor_keys, start=1):
            identity, label, context = known_people[actor_key]
            suffix = context.casefold()
            if repeated_context:
                suffix = f"{suffix} · person {index}"
            known_people[actor_key] = (identity, f"{label} · {suffix}", context)

    built: list[tuple[int, JournalEntry]] = []
    for (category, stable_id), records in grouped.items():
        records.sort(key=lambda record: (event_order.get(record[2], 0), record[0].event_id or 0))
        first_fact, first_public, first_event_id = records[0]
        latest_fact, _, _ = records[-1]
        safe_names = [record[1]["name"] for record in records if record[1]["name"]]
        title = (
            known_people[stable_id][1]
            if category == "person"
            else safe_names[-1] if safe_names else _TITLE[category]
        )
        origin_event_id = first_event_id
        if first_fact.origin_turn_id:
            origin_event_id = turn_events.get(first_fact.origin_turn_id, origin_event_id)
        origin_turn = event_order.get(origin_event_id) if origin_event_id is not None else None
        origin_context = place_at_event.get(origin_event_id) if origin_event_id is not None else None
        origin_place = origin_context[0] if origin_context else None
        origin_place_entry_id = origin_context[1] if origin_context else None
        actor: str | None = None
        actor_ref_id: str | None = None
        related_entry_id: str | None = None
        related_actor_id: str | None = None
        if category == "promise" and first_fact.promise_actor is not None:
            actor_key = actor_identity_id(session_id, first_fact.promise_actor)
            actor_record = known_people.get(actor_key)
            if actor_record is not None:
                related_entry_id, actor, _ = actor_record
                related_actor_id = related_entry_id
                actor_ref_id = related_entry_id

        related_entry_ids: list[str] = []
        if category == "consequence" and first_fact.target.kind == "object":
            object_clue_id = known_object_clues.get(story_target_id(first_fact.target))
            if object_clue_id:
                related_entry_ids.append(object_clue_id)
            if origin_place_entry_id:
                related_entry_ids.append(origin_place_entry_id)
        ownership: str | None = None
        related_object = getattr(first_fact, "related_object", None)
        if category in {"clue", "hint"} and related_object is not None:
            ownership = object_ownership.get(story_target_id(related_object), "untracked")

        history: list[JournalStatusChange] = []
        if category == "clock":
            # A later current-value reveal does not disclose earlier private update counts.
            # Keep the original qualitative observation, then only explicitly public updates.
            history = [JournalStatusChange(
                status=_safe_status(first_fact) or "advanced",  # type: ignore[arg-type]
                turn_number=origin_turn,
                history_event_id=origin_event_id,
            )]
            for fact, _, accessible_event_id in records[1:]:
                disclosure = fact.clock_disclosure
                if disclosure is None or not (disclosure.current or disclosure.completion):
                    continue
                item = JournalStatusChange(
                    status=_safe_status(fact) or "advanced",  # type: ignore[arg-type]
                    turn_number=event_order.get(accessible_event_id) if accessible_event_id is not None else None,
                    history_event_id=accessible_event_id,
                )
                if history[-1] != item:
                    history.append(item)
        else:
            place_visited = False
            for fact, _, accessible_event_id in records:
                # Learning the name of a place after visiting it cannot turn an explored
                # location back into an unvisited location.
                if category == "place" and fact.kind == "learn_place" and place_visited:
                    continue
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
                if category == "place" and fact.kind in {"visit", "move"} and status == "visited":
                    place_visited = True
        current_status = _safe_status(latest_fact)
        if category == "place" and any(fact.kind in {"visit", "move"} for fact, _, _ in records):
            current_status = "visited"
        if current_status is None:
            continue
        clock_disclosure = getattr(latest_fact, "clock_disclosure", None)
        clock_is_current = category == "clock" and clock_disclosure is not None and clock_disclosure.current
        clock_maximum = clock_disclosure.maximum if category == "clock" and clock_disclosure else None
        quantity = latest_fact.quantity if clock_is_current else None
        citation = _citation(first_public)
        identity_id = (
            actor_entry_id(session_id, first_fact.target)
            if category == "person" and first_fact.target.kind == "entity"
            else journal_entry_id(session_id, category, stable_id)
        )
        actor_aliases = []
        if category == "person":
            actor_aliases = list({name.casefold(): name for name in safe_names}.values())
        entry = JournalEntry(
            id=identity_id,
            actor_id=identity_id if category == "person" else None,
            actor_aliases=actor_aliases,
            actor_reference_event_ids=(
                sorted(actor_reference_events.get(stable_id, set()))
                if category == "person"
                else []
            ),
            related_actor_id=related_actor_id,
            category=category,  # type: ignore[arg-type]
            title=title,
            # Preserve the first exact earned text as the immutable original. Status changes
            # remain separate in history, so later clues or summaries cannot replace it.
            text=first_public["text"],
            status=current_status,  # type: ignore[arg-type]
            # Candidate counts are withheld until the ledger gets an explicit player reveal.
            quantity=quantity,
            clock_maximum=clock_maximum,
            ownership=ownership,  # type: ignore[arg-type]
            origin=JournalOrigin(
                turn_number=origin_turn,
                history_event_id=origin_event_id,
                place=origin_place,
                actor=actor,
                actor_id=actor_ref_id,
                citation=citation,
            ),
            history=history,
            related_entry_id=related_entry_id,
            related_entry_ids=related_entry_ids,
        )
        # Undisclosed clock changes must not leak through the journal ordering either.
        ordered_records = records if category != "clock" or clock_is_current else records[:1]
        latest_sequence = max(
            (fact_order[fact.effect_id] for fact, _, _ in ordered_records),
            default=0,
        )
        built.append((latest_sequence, entry))

    built.sort(key=lambda item: item[0], reverse=True)
    entries = [entry for _, entry in built]
    current_place = next(
        (entry for _, entry in built if entry.id == latest_place_entry_id),
        None,
    )
    active_goal = next(
        (entry for _, entry in built if entry.category == "goal" and entry.status == "active"),
        None,
    )
    first_payload = (events[0].payload or {}) if events else {}
    # Entries keep journal-era sessions available; the marker only rescues new sessions that
    # have no committed facts yet, so unmarked pre-journal sessions with no entries stay legacy.
    tracked = (
        not events
        or bool(entries)
        or first_payload.get("journal_tracking_version") == JOURNAL_TRACKING_VERSION
    )
    tracking = "available" if tracked else "legacy_untracked"
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
                        *entry.actor_aliases,
                    ),
                )
            ).casefold()
        ]
    return journal.model_copy(update={"entries": entries})
