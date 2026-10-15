"""Player-safe session journal response contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
]
JournalOwnership = Literal[
    "carried",
    "left_behind",
    "with_another_character",
    "consumed",
    "untracked",
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
    clock_maximum: int | None = None
    ownership: JournalOwnership | None = None
    origin: JournalOrigin
    history: list[JournalStatusChange]
    related_entry_id: str | None
    related_entry_ids: list[str] = Field(default_factory=list)


class JournalRead(JournalRecord):
    session_id: int
    tracking: Literal["available", "legacy_untracked"]
    entries: list[JournalEntry]
    current_place: JournalEntry | None
    active_goal: JournalEntry | None
