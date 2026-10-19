"""Typed envelopes for the per-session broadcast channel (EDG-047).

Every event a member can receive is one ``ChannelEnvelope``. ``seq`` is the session's monotonic
sequence number (the SSE ``id``), ``channel`` says what kind of table information it is, and
``actor`` says who acted and as which character. The audience (who may receive the event) is
decided at publish time and stored with the event, but it is never sent to a client; the server
filters on it for live delivery and for replay. See docs/session-broadcast.md.

The event registry below is the allowlist: an event type that is not registered here cannot be
published, so a new kind of data cannot reach members by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Channel = Literal["table", "narration", "rolls", "system", "private"]
PresenceState = Literal["active", "idle"]

# Audience kinds. ``player`` is resolved at publish time to ``player:<user_id>`` (the seated player
# who acted), or to ``host`` when there is no account (single-user local mode).
#   table    every campaign member: the owner, players and spectators
#   host     the campaign owner only
#   player   the one seated player the event is about, and nobody else (not even the owner)
#   control  server-internal wake-up for access changes; never delivered to any client
AUDIENCE_TABLE = "table"
AUDIENCE_HOST = "host"
AUDIENCE_CONTROL = "control"
AUDIENCE_PLAYER_PREFIX = "player:"


class ChannelActor(BaseModel):
    """Who acted: the account and the character they played. Display name is the only identity."""

    model_config = ConfigDict(extra="forbid")

    user_id: int | None = None
    display_name: str | None = None
    character_id: int | None = None
    character_name: str | None = None


class ChannelEnvelope(BaseModel):
    """One ordered event on the session channel, as delivered to a client."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    type: str
    channel: Channel
    actor: ChannelActor | None = None
    turn_id: str | None = None
    at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class PresenceEntry(BaseModel):
    """One member as shown on the roster. Display name and seated character only, never email."""

    user_id: int
    display_name: str | None = None
    character_id: int | None = None
    character_name: str | None = None
    state: PresenceState = "active"


class PresenceUpdate(BaseModel):
    """Client-reported attention: a member tabbed away reports ``idle``."""

    model_config = ConfigDict(extra="forbid")

    state: PresenceState


# Registry: event type -> (channel, audience kind). Audience kind ``player`` is resolved from the actor.
EVENT_TYPES: dict[str, tuple[Channel, str]] = {
    # What the acting player submitted, visible to the whole table as the turn starts.
    "turn.action": ("table", AUDIENCE_TABLE),
    # Progress stages of the acting player's turn (parsing, adjudicating, narrating, saving).
    "turn.stage": ("system", AUDIENCE_TABLE),
    # Committed narration and the citations the table may read.
    "narration.committed": ("narration", AUDIENCE_TABLE),
    # Committed dice outcome, without the private rest evidence.
    "rolls.committed": ("rolls", AUDIENCE_TABLE),
    # Shared scene and encounter state (public combat roster only).
    "state.committed": ("system", AUDIENCE_TABLE),
    # The end of a turn: committed, not committed, or unknown (clients then re-read state).
    "turn.finished": ("system", AUDIENCE_TABLE),
    # The acting player's own character projection and rest evidence. Never sent to anyone else.
    "character.state": ("private", "player"),
    # A failed turn's safe error, for the player who submitted it only.
    "turn.failed": ("private", "player"),
    # The turn spotlight changed hands or its rules did (EDG-048): the new holder, the reason, the
    # version and the next-up order, all as the table may read them. Never player-private.
    "turn.spotlight_changed": ("system", AUDIENCE_TABLE),
    # A queued action was added, edited, cancelled, played or dropped (EDG-051). The queue as the
    # table may read it: who has one and its status, never the text. Carries a `revision`.
    "turn.queue_changed": ("system", AUDIENCE_TABLE),
    "presence.joined": ("system", AUDIENCE_TABLE),
    "presence.left": ("system", AUDIENCE_TABLE),
    "presence.idle": ("system", AUDIENCE_TABLE),
    "presence.active": ("system", AUDIENCE_TABLE),
    # Server-internal: a membership or seat changed, so every open subscriber re-checks access.
    "access.changed": ("system", AUDIENCE_CONTROL),
}
