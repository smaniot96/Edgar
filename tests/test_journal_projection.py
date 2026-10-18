"""Privacy boundaries for the player-safe session journal projection."""

import hashlib
from datetime import UTC, datetime, timedelta

from apps.api.services.journal import (
    filter_player_journal,
    journal_tracking_marker,
    project_player_journal,
)
from db.postgres.models import EventLog
from edgar_core.story import StoryFact, StoryRef, story_target_id


def _fact(
    *,
    kind: str,
    canonical_id: str,
    event_id: int,
    turn_id: str,
    status: str = "discovered",
    text: str | None = None,
    quantity: int | None = None,
    audience: str = "player",
    origin_turn_id: str | None = None,
) -> StoryFact:
    target = StoryRef(
        collection="synthetic",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        kind={"objective": "objective", "clock": "clock", "visit": "scene"}.get(kind, "entity"),
        canonical_id=canonical_id,
    )
    return StoryFact(
        kind=kind,
        target=target,
        status=status,
        source_reference_ids=[],
        knowledge_fact_ids=[],
        player_text=text,
        quantity=quantity,
        destination=None,
        operation=None,
        promise_actor=None,
        effect_id=hashlib.sha256(f"{event_id}:{canonical_id}".encode()).hexdigest(),
        turn_id=turn_id,
        display_name="",
        source_refs=[
            {
                "audience": audience,
                "reference_id": "d" * 64,
                "source_sha256": "a" * 64,
                "extraction_sha256": "e" * 64,
                "manifest_sha256": "b" * 64,
                "collection": "synthetic",
                "page": 7,
                "page_end": 8,
                "private_path": "/secret/prepared/source.pdf",
            }
        ],
        evidence_kind="source",
        origin_turn_id=origin_turn_id,
        fact_id=story_target_id(target),
        event_id=event_id,
    )


def _event(
    event_id: int, session_id: int, turn_id: str, created_at: datetime, *, tracked: bool = True
) -> EventLog:
    payload = {"turn_id": turn_id, **(journal_tracking_marker() if tracked else {})}
    return EventLog(
        id=event_id,
        session_id=session_id,
        event_type="narration",
        payload=payload,
        created_at=created_at,
    )


def test_projection_is_session_scoped_and_exposes_only_allowlisted_origins() -> None:
    now = datetime.now(UTC)
    facts = [
        _fact(kind="visit", canonical_id="old_room", event_id=11, turn_id="turn-1", text="The old room"),
        _fact(kind="discover", canonical_id="note", event_id=12, turn_id="turn-2", text="The lens tracks reflections."),
        _fact(kind="discover", canonical_id="hidden", event_id=99, turn_id="other", text="A distant gate opens."),
        _fact(
            kind="discover",
            canonical_id="unverified_source",
            event_id=12,
            turn_id="turn-2",
            audience="planner",
            text="NOT PLAYER KNOWLEDGE",
        ),
    ]
    result = project_player_journal(
        1,
        {"facts": [fact.model_dump() for fact in facts]},
        [
            _event(11, 1, "turn-1", now),
            _event(12, 1, "turn-2", now + timedelta(minutes=1)),
            _event(99, 2, "other", now + timedelta(minutes=2)),
        ],
    )

    assert [entry.text for entry in result.entries] == [
        "The lens tracks reflections.",
        "The old room",
        "A distant gate opens.",
    ]
    assert result.entries[0].origin.turn_number == 2
    assert result.entries[0].origin.history_event_id == 12
    assert result.entries[0].origin.citation == "Pages 7–8"
    serialized = result.model_dump_json()
    unverified_event = next(entry for entry in result.entries if entry.text == "A distant gate opens.")
    assert unverified_event.origin.history_event_id is None
    assert unverified_event.history[0].history_event_id is None
    for private_value in ("NOT PLAYER KNOWLEDGE", "private_path", "/secret/"):
        assert private_value not in serialized


def test_new_session_without_committed_facts_is_available_and_empty() -> None:
    now = datetime.now(UTC)
    private = _fact(
        kind="discover",
        canonical_id="hidden",
        event_id=31,
        turn_id="turn-1",
        audience="planner",
        text="A hidden fact that the player has not earned.",
    )
    events = [_event(31, 4, "turn-1", now)]

    private_only = project_player_journal(4, {"facts": [private.model_dump()]}, events)
    no_facts = project_player_journal(4, {"facts": []}, events)

    assert private_only.entries == []
    assert private_only.tracking == "available"
    assert no_facts.entries == []
    assert no_facts.current_place is None
    assert no_facts.active_goal is None
    assert no_facts.tracking == "available"


def test_session_without_narration_rows_is_available() -> None:
    assert project_player_journal(4, {"facts": []}, []).tracking == "available"


def test_pre_journal_session_with_entries_stays_available() -> None:
    now = datetime.now(UTC)
    known = _fact(
        kind="discover",
        canonical_id="known",
        event_id=32,
        turn_id="turn-2",
        text="A known clue.",
    )
    events = [
        _event(31, 4, "turn-1", now, tracked=False),
        _event(32, 4, "turn-2", now + timedelta(minutes=1), tracked=True),
    ]

    result = project_player_journal(4, {"facts": [known.model_dump()]}, events)

    assert [entry.text for entry in result.entries] == ["A known clue."]
    assert result.tracking == "available"


def test_pre_journal_session_without_entries_stays_legacy() -> None:
    now = datetime.now(UTC)
    result = project_player_journal(4, {"facts": []}, [_event(31, 4, "turn-1", now, tracked=False)])

    assert result.entries == []
    assert result.tracking == "legacy_untracked"


def test_private_clock_completion_never_reveals_progress_or_terminal_state() -> None:
    now = datetime.now(UTC)
    clock = _fact(
        kind="clock",
        canonical_id="secret_clock",
        event_id=21,
        turn_id="turn-1",
        status="completed",
        text="The storm gathers over the harbor.",
        quantity=6,
    )
    result = project_player_journal(
        3,
        {"facts": [clock.model_dump()]},
        [_event(21, 3, "turn-1", now)],
    )

    entry = result.entries[0]
    assert entry.category == "clock"
    assert entry.status == "advanced"
    assert entry.quantity is None
    assert [change.status for change in entry.history] == ["advanced"]
    assert len(entry.history) == 1


def test_filters_apply_only_to_player_projection() -> None:
    now = datetime.now(UTC)
    fact = _fact(
        kind="objective",
        canonical_id="rescue",
        event_id=31,
        turn_id="turn-1",
        text="Rescue the ferryman.",
        status="active",
    )
    journal = project_player_journal(4, {"facts": [fact.model_dump()]}, [_event(31, 4, "turn-1", now)])

    assert [entry.category for entry in filter_player_journal(journal, category="goal").entries] == ["goal"]
    assert [entry.text for entry in filter_player_journal(journal, query="FERRYMAN").entries] == [
        "Rescue the ferryman."
    ]
    assert filter_player_journal(journal, query="hidden future").entries == []
