"""Turn-to-channel mapping: what a turn publishes to the whole table (EDG-047).

A turn publishes only from an explicit allowlist. Committed output is split by audience:

* ``turn.action``, ``turn.stage``, ``narration.committed``, ``rolls.committed``, ``state.committed``
  and ``turn.finished`` go to the table (owner, players and spectators).
* ``character.state`` and ``turn.failed`` go only to the seated player who acted. In a party fight
  (EDG-049) ``character.state`` also goes, once per hero the turn changed, to that hero's own
  controller and nobody else; ``state.committed`` then omits the acting hero's pending declaration, and
  ``combat.turn_changed`` tells the table whose turn is next.

Not published at all: streamed narration tokens (uncommitted text), the adjudication frame (it can
carry engine reasoning), the ``debug`` frame (developer diagnostics), and the roll's private
``rest`` evidence on the table channel. Publishing is best effort and never raises into the turn:
a Redis failure is logged by event name and the turn still commits and answers its caller.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from redis.asyncio import Redis

from apps.api.schemas.channel import ChannelActor
from apps.api.services.safe_errors import classify_error, log_failure

from . import session_channel
from .turn_runner import table_combat_state

log = structlog.get_logger()

_PUBLISH_TIMEOUT_SECONDS = 2.0
_OUTCOMES = {"committed", "not_committed", "unknown"}


def parse_sse_frame(chunk: str) -> tuple[str, Any]:
    """The event name and decoded JSON data of one SSE frame produced by the turn runner."""
    name = "message"
    data: Any = None
    for line in chunk.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: ") :])
    return name, data


def _public_roll(roll: dict[str, Any] | None) -> dict[str, Any] | None:
    if roll is None:
        return None
    return {key: value for key, value in roll.items() if key != "rest"}


class TurnBroadcast:
    """Publishes one turn's lifecycle for one session. Create it per turn; call ``start`` once."""

    def __init__(
        self,
        redis: Redis,
        session_id: int,
        *,
        actor: ChannelActor,
        turn_id: str | None,
    ) -> None:
        self._redis = redis
        self._session_id = session_id
        self._actor = actor
        self._turn_id = turn_id
        self._open = False
        # EDG-049: the hero whose idle turn this was, when the turn is the default Dodge of a skip.
        self.skipped_character_id: int | None = None

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(
                session_channel.publish(
                    self._redis,
                    self._session_id,
                    kind=event_type,
                    actor=self._actor,
                    turn_id=self._turn_id,
                    data=data,
                ),
                timeout=_PUBLISH_TIMEOUT_SECONDS,
            )
        except Exception:
            log_failure(
                "session_channel_publish_failed",
                classify_error("internal_fault"),
                level="warning",
                session_id=self._session_id,
                turn_id=self._turn_id,
            )

    async def start(self, message: str) -> None:
        """The acting player's submitted action, shared with the table as the turn begins."""
        self._open = True
        await self._emit("turn.action", {"text": message})

    async def observe(self, chunk: str) -> None:
        """Feed one frame the turn runner yielded. Only progress and error frames matter here."""
        if not self._open:
            return
        if chunk.startswith("event: status"):
            _name, data = parse_sse_frame(chunk)
            await self._emit("turn.stage", {"stage": (data or {}).get("stage")})
        elif chunk.startswith("event: error"):
            _name, data = parse_sse_frame(chunk)
            body = data if isinstance(data, dict) else {}
            await self._emit("turn.failed", {"code": body.get("code"), "message": body.get("message")})
            await self.finish(_outcome(body.get("outcome")))

    async def committed(self, done: dict[str, Any], side: dict[str, Any] | None = None) -> None:
        """Publish the committed turn: narration, dice, shared state, then the player's own state.

        `side` (EDG-049) holds the other heroes' sheets a party fight changed; each goes only to its own
        controller.
        """
        if not self._open:
            return
        await self._emit(
            "narration.committed",
            {
                "event_id": done.get("event_id"),
                "narration": done.get("narration"),
                "citations": done.get("citations", []),
                "ruling_citation_ids": done.get("ruling_citation_ids", []),
                "rules_edition": done.get("rules_edition"),
            },
        )
        roll = done.get("roll")
        await self._emit(
            "rolls.committed",
            {"roll_status": done.get("roll_status"), "roll": _public_roll(roll)},
        )
        await self._emit(
            "state.committed",
            {
                "current_scene_id": done.get("current_scene_id"),
                "combat_state": table_combat_state(done.get("combat_state")),
                "campaign_complete": done.get("campaign_complete"),
            },
        )
        combat = done.get("combat_state") or {}
        if combat.get("party"):
            await self._emit(
                "combat.turn_changed",
                {
                    "round": combat.get("round"),
                    "ended": bool(combat.get("ended")),
                    "outcome": combat.get("outcome"),
                    "current_turn": combat.get("current_turn"),
                    "current_turn_character_id": combat.get("current_turn_character_id"),
                    "skipped_character_id": self.skipped_character_id,
                },
            )
        await self._emit(
            "character.state",
            {
                "character": done.get("character"),
                "rest": (roll or {}).get("rest"),
            },
        )
        for other in (side or {}).get("party_sheets") or []:
            await self._emit_private_sheet(other)
        self._open = False
        await self._emit_finished("committed", done.get("event_id"))

    async def _emit_private_sheet(self, other: dict[str, Any]) -> None:
        """One other hero's changed sheet, addressed to the account that controls it (audience: that player)."""
        sheet = other.get("character") or {}
        try:
            await asyncio.wait_for(
                session_channel.publish(
                    self._redis,
                    self._session_id,
                    kind="character.state",
                    actor=ChannelActor(
                        user_id=other.get("user_id"),
                        character_id=other.get("character_id"),
                        character_name=sheet.get("name"),
                    ),
                    turn_id=self._turn_id,
                    data={"character": sheet, "rest": None},
                ),
                timeout=_PUBLISH_TIMEOUT_SECONDS,
            )
        except Exception:
            log_failure(
                "session_channel_publish_failed",
                classify_error("internal_fault"),
                level="warning",
                session_id=self._session_id,
                turn_id=self._turn_id,
            )

    async def finish(self, outcome: str) -> None:
        """Close a turn that did not commit (or whose outcome is unknown). No-op once closed."""
        if not self._open:
            return
        self._open = False
        await self._emit_finished(_outcome(outcome), None)

    async def _emit_finished(self, outcome: str, event_id: int | None) -> None:
        await self._emit("turn.finished", {"outcome": outcome, "event_id": event_id})


def _outcome(value: Any) -> str:
    """Only the three documented outcomes are published; anything else is ``unknown``."""
    return value if isinstance(value, str) and value in _OUTCOMES else "unknown"


async def notify_access_changed(redis: Redis, session_ids: list[int]) -> None:
    """Wake every open subscriber of these sessions so each re-checks its access at once.

    Called after a membership or seat change has committed. A failure here is covered by the
    subscribers' periodic re-check, so it is logged and never raised.
    """
    for session_id in session_ids:
        try:
            await asyncio.wait_for(
                session_channel.publish(
                    redis,
                    session_id,
                    kind="access.changed",
                    actor=None,
                    turn_id=None,
                    data={},
                ),
                timeout=_PUBLISH_TIMEOUT_SECONDS,
            )
        except Exception:
            log_failure(
                "session_access_notify_failed",
                classify_error("internal_fault"),
                level="warning",
                session_id=session_id,
            )
