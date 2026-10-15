"""Player-safe session journal response contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

JournalCategory = Literal[
    "goal",
    "milestone",
    "clue",
    "hint",
    "person",
    "place",
    "promise",
    "clock",
    "consequence",
]
JournalStatus = Literal[
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
]


class JournalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class JournalStatusChange(JournalRecord):
    status: JournalStatus
    turn_number: int | None
    history_event_id: int | None


class JournalOrigin(JournalRecord):
    """Earned, readable origin fields; private source/version identities are omitted."""

    turn_number: int | None
    history_event_id: int | None
    place: str | None
    actor: str | None
    citation: str | None


class JournalEntry(JournalRecord):
    id: str
    category: JournalCategory
    title: str
    text: str | None
    status: JournalStatus
    quantity: int | None
    origin: JournalOrigin
    history: list[JournalStatusChange]
    related_entry_id: str | None


class JournalRead(JournalRecord):
    session_id: int
    tracking: Literal["available", "legacy_untracked"]
    entries: list[JournalEntry]
    current_place: JournalEntry | None
    active_goal: JournalEntry | None
