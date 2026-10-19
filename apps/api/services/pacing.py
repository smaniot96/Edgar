"""Table pacing policy and the queued-action data rules (EDG-051).

Two ideas, both enforced on the server under the same session-row lock as the EDG-048 spotlight:

* **Pacing policy**: one named policy per session, readable by every member. It fixes how long an
  idle holder keeps the spotlight, whether an absent holder is passed early (auto-pass), and how
  long a queued action stays valid. ``relaxed`` suits play-by-post, ``standard`` is the EDG-048
  default (300 s, so existing tables are unchanged), ``fast`` suits a live table. ``custom`` is what
  a table becomes when the owner sets an explicit timeout.
* **Queued action**: a seated player whose turn is not up may queue ONE intended action. It never
  moves the spotlight and is never a turn: it becomes one only when its author, holding the
  spotlight, submits it through the normal turn gate. The server then validates it again against
  the current state (same text and version, not expired, same scene, combat flag and life state it
  was queued under, same character) and refuses a stale one with a clear code instead of running it.
  It is consumed by the turn's own commit, so a turn can neither lose nor duplicate it.

This module holds the pure rules and the stored shape. The operations that lock the table and talk
to the spotlight live in ``queued_actions.py``. Rationale and rejected alternatives:
docs/session-broadcast.md (pacing section).

Stored shape, ``sessions.queued_actions``: ``{"revision": int, "items": {"<user_id>": item}}`` with
``item = {id, user_id, character_id, text, version, basis, request_id, created_at, updated_at,
expires_at}``. ``revision`` increases on every change so clients can ignore an out-of-order event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from agent.death import life_state_of
from db.postgres.models import Session as SessionModel

from .character_assignments import character_play_state
from .combat import load_active_combat

POLICY_RELAXED = "relaxed"
POLICY_STANDARD = "standard"
POLICY_FAST = "fast"
POLICY_CUSTOM = "custom"
POLICIES = (POLICY_RELAXED, POLICY_STANDARD, POLICY_FAST)
DEFAULT_POLICY = POLICY_STANDARD
MAX_QUEUED_TEXT = 2000
AUTO_PASS_GRACE = timedelta(seconds=20)
EVENT_TYPE = "turn.queue_changed"


@dataclass(frozen=True)
class Policy:
    """What a named policy means. ``timeout_seconds`` is None for ``custom`` (the stored value stands)."""

    name: str
    label: str
    summary: str
    timeout_seconds: int | None
    auto_pass: bool
    queue_ttl_seconds: int


_POLICIES: dict[str, Policy] = {
    POLICY_RELAXED: Policy(
        POLICY_RELAXED,
        "Relaxed",
        "Play at your own pace: an idle holder keeps the spotlight for an hour, and queued actions last a day.",
        3600,
        False,
        86400,
    ),
    POLICY_STANDARD: Policy(
        POLICY_STANDARD,
        "Standard",
        "An idle holder keeps the spotlight for five minutes, and queued actions last an hour.",
        300,
        False,
        3600,
    ),
    POLICY_FAST: Policy(
        POLICY_FAST,
        "Fast",
        "A holder who has left the table is passed after 20 seconds, an idle one after a minute, "
        "and queued actions last ten minutes.",
        60,
        True,
        600,
    ),
    POLICY_CUSTOM: Policy(
        POLICY_CUSTOM,
        "Custom",
        "The host set a custom turn timeout. Queued actions last an hour.",
        None,
        False,
        3600,
    ),
}


def policy_of(session: SessionModel) -> Policy:
    return _POLICIES.get(session.pacing_policy or DEFAULT_POLICY, _POLICIES[DEFAULT_POLICY])


def preset(name: str) -> Policy:
    return _POLICIES[name]


def auto_pass(session: SessionModel) -> bool:
    return policy_of(session).auto_pass


def pacing_view(session: SessionModel) -> dict[str, Any]:
    """The policy as every member reads it. Always the effective timeout, never a stale preset value."""
    policy = policy_of(session)
    return {
        "policy": policy.name,
        "label": policy.label,
        "summary": policy.summary,
        "timeout_seconds": session.spotlight_timeout_seconds,
        "auto_pass": policy.auto_pass,
        "queue_ttl_seconds": policy.queue_ttl_seconds,
    }


# --- Stored queue ------------------------------------------------------------------------------


def _store(session: SessionModel) -> dict[str, Any]:
    raw = session.queued_actions if isinstance(session.queued_actions, dict) else {}
    items = raw.get("items") if isinstance(raw.get("items"), dict) else {}
    revision = raw.get("revision") if isinstance(raw.get("revision"), int) else 0
    return {"revision": revision, "items": dict(items)}


def revision_of(session: SessionModel) -> int:
    return _store(session)["revision"]


def parse_time(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def is_expired(item: dict[str, Any], now: datetime) -> bool:
    expires = parse_time(item.get("expires_at"))
    return expires is None or now >= expires


def get_item(session: SessionModel, user_id: int) -> dict[str, Any] | None:
    return _store(session)["items"].get(str(user_id))


def find_item(session: SessionModel, action_id: str) -> dict[str, Any] | None:
    for item in _store(session)["items"].values():
        if item.get("id") == action_id:
            return item
    return None


def save(session: SessionModel, items: dict[str, Any]) -> None:
    """Persist ``items`` with the next revision. Always assigns a new dict so the JSON column is flushed."""
    session.queued_actions = {"revision": revision_of(session) + 1, "items": items}


def purge_expired(session: SessionModel, now: datetime) -> bool:
    """Drop expired items (called by every writer, so a clock never needs a background job)."""
    items = _store(session)["items"]
    kept = {key: item for key, item in items.items() if not is_expired(item, now)}
    if len(kept) == len(items):
        return False
    save(session, kept)
    return True


def new_item(
    session: SessionModel,
    *,
    user_id: int,
    character_id: int,
    text: str,
    basis: dict[str, Any],
    request_id: str | None,
    now: datetime,
) -> dict[str, Any]:
    ttl = timedelta(seconds=policy_of(session).queue_ttl_seconds)
    return {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "character_id": character_id,
        "text": text,
        "version": 1,
        "basis": basis,
        "request_id": request_id,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "expires_at": (now + ttl).isoformat(),
    }


def prune_unseated(session: SessionModel, controllers: dict[int, int]) -> bool:
    """Drop items whose player no longer controls the queued character. ``controllers``: character id -> user id."""
    items = _store(session)["items"]
    kept = {
        key: item
        for key, item in items.items()
        if controllers.get(item.get("character_id")) == item.get("user_id")
    }
    if len(kept) == len(items):
        return False
    save(session, kept)
    return True


# --- Validation against the situation ----------------------------------------------------------


async def basis_of(db: Any, session: SessionModel, character_id: int) -> dict[str, Any]:
    """The situation a queued action was written against: scene, whether combat is on, the hero's life state."""
    state = await character_play_state(db, character_id, session.campaign_id)
    return {
        "scene": session.current_scene_id,
        "combat": await load_active_combat(db, session.id) is not None,
        "life": life_state_of(state.get("stats")) if state else "unknown",
    }


def stale_reason(item: dict[str, Any], current: dict[str, Any]) -> str | None:
    saved = item.get("basis") or {}
    if saved.get("scene") != current.get("scene"):
        return "scene_changed"
    if saved.get("combat") != current.get("combat"):
        return "combat_changed"
    if saved.get("life") != current.get("life"):
        return "hero_state_changed"
    return None


def item_status(item: dict[str, Any], current: dict[str, Any] | None, now: datetime) -> tuple[str, str | None]:
    """Derived status: expired, stale (with the reason) or queued. Stored items are only ever 'queued'."""
    if is_expired(item, now):
        return "expired", None
    if current is not None:
        reason = stale_reason(item, current)
        if reason is not None:
            return "stale", reason
    return "queued", None


def check_claim(
    item: dict[str, Any] | None,
    *,
    action_id: str,
    version: int | None,
    message: str,
    holder: int | None,
    current: dict[str, Any],
    now: datetime,
) -> str | None:
    """The refusal code for playing a queued action now, or None when it may run. Order matters."""
    if item is None or item.get("id") != action_id:
        return "queued_action_missing"
    if (version is not None and version != item.get("version")) or message != item.get("text"):
        return "queued_action_changed"
    if is_expired(item, now):
        return "queued_action_expired"
    if item.get("character_id") != holder or stale_reason(item, current) is not None:
        return "queued_action_stale"
    return None


def consume(session: SessionModel, user_id: int, action_id: str, version: int | None) -> str:
    """Remove the played item in the turn's own commit. Returns 'consumed', 'edited' or 'gone'.

    'edited' (the player changed the text while the turn ran) keeps the newer item: it is a new intent.
    'gone' (already played by another turn, or cancelled meanwhile) means the turn must not commit.
    """
    items = _store(session)["items"]
    item = items.get(str(user_id))
    if item is None or item.get("id") != action_id:
        return "gone"
    if version is not None and item.get("version") != version:
        return "edited"
    items.pop(str(user_id))
    save(session, items)
    return "consumed"


def queue_view(
    session: SessionModel,
    members: dict[int, Any],
    current_by_character: dict[int, dict[str, Any]],
    now: datetime,
    *,
    viewer_user_id: int | None = None,
    include_expired_for_viewer: bool = True,
) -> dict[str, Any]:
    """The queue as shown to the table. Text is included only for its author; others see that it is queued.

    A player sees their own expired item (so the composer can say so); the table never does.
    """
    store = _store(session)
    shown: list[dict[str, Any]] = []
    for key in sorted(store["items"], key=int):
        item = store["items"][key]
        status, reason = item_status(item, current_by_character.get(item.get("character_id")), now)
        mine = viewer_user_id is not None and item.get("user_id") == viewer_user_id
        if status == "expired" and not (mine and include_expired_for_viewer):
            continue
        member = members.get(item.get("character_id"))
        view: dict[str, Any] = {
            "id": item["id"],
            "user_id": item["user_id"],
            "display_name": getattr(member, "display_name", None),
            "character_id": item["character_id"],
            "character_name": getattr(member, "name", None),
            "status": status,
            "stale_reason": reason,
            "version": item["version"],
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
            "expires_at": item["expires_at"],
        }
        if mine:
            view["text"] = item["text"]
        shown.append(view)
    return {"revision": store["revision"], "items": shown}
