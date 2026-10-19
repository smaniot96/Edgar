"""Server-side spotlight arbitration for multi-PC tables, out of combat (EDG-048).

The spotlight is the single turn token of a shared table: whoever holds it may submit a turn, and
nobody else may until it passes. The holder is `sessions.active_character_id` (unchanged meaning);
this module owns every change to it.

Rules, per session (see docs/session-broadcast.md for the wire contract):

* Engaged table: the rotation has at least two characters (seated players, plus the campaign owner's
  own heroes once they have held the spotlight). Solo and single-PC tables are never engaged: no
  events, no refusals beyond the membership gate, exactly the behaviour before this module.
* ``round_robin`` (default): the holder's committed turn passes the spotlight to the next member of
  the rotation who is present on the channel (EDG-047 presence). Absent members are skipped. With
  nobody else present the spotlight stays with the holder, so it never lands on an empty chair.
* ``free_for_all``: the spotlight is free between turns. A seated member whose turn arrives while it is
  free claims it; the claim is decided under the row lock, so two simultaneous claims yield one winner.
  After a turn the mover waits a short per-character cooldown before claiming again.
* ``owner_directed``: nothing passes by itself. The owner hands the spotlight over (or back).
* Timeout: a holder idle for ``timeout_seconds`` (no turn started, no claim) loses it. Round robin hands
  it to the next present member; free-for-all releases it; owner-directed never times out. The handover
  is applied by the next request that takes the table lock (a turn, an intro or an owner change).
* Release: a removed, left or demoted member's seat leaves the rotation in the same transaction, and a
  spotlight held by that seat is released (round robin hands it on at once). Spectators have no seat,
  so they are never in the rotation and never hold the spotlight.
* Every holder, rotation, mode or timeout change bumps ``spotlight_version`` and is announced once, after
  it commits, as ``turn.spotlight_changed`` to the table audience, while the table lock is still held,
  so events arrive in version order.

Concurrency: every mutation runs while the caller holds the per-session turn lock (Redis, cross-worker)
and inside one transaction that locks the session row (``SELECT ... FOR UPDATE``). The turn's own commit
re-checks ``spotlight_version`` and the holder on the locked row before it applies the pass, so a
turn can never commit against a spotlight that changed while the model was thinking.

* Combat (EDG-049): while a shared fight with two or more heroes is running, the initiative order
  decides the holder, not the rotation. ``sessions.active_character_id`` is kept equal to the hero
  whose slot is next (the combat turn holder); only that hero's controller may act, with the same
  ``spotlight_version`` gate. Every committed combat turn bumps the version and passes the turn to the
  next hero who can act. An idle holder is handled by ``open_skip`` (docs/party-combat.md): the owner
  may skip at any time, any seated player once the holder is overdue or absent, and the skipped hero
  takes the Dodge action by default.

Alternatives considered and rejected are documented in docs/session-broadcast.md (spotlight section).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import db.postgres.session as db_session_module
from agent.death import life_state_of
from agent.party_combat import is_party
from db.postgres.models import (
    Campaign,
    Character,
    Session as SessionModel,
    SessionParticipant,
    User,
)

from ..schemas.channel import ChannelActor
from . import pacing, session_channel
from .character_assignments import character_play_state
from .combat import load_active_combat
from .safe_errors import classify_error, log_failure

log = structlog.get_logger()

MODE_ROUND_ROBIN = "round_robin"
MODE_OWNER_DIRECTED = "owner_directed"
MODE_FREE_FOR_ALL = "free_for_all"
MODES = (MODE_ROUND_ROBIN, MODE_OWNER_DIRECTED, MODE_FREE_FOR_ALL)
DEFAULT_MODE = MODE_ROUND_ROBIN
DEFAULT_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 3600
FREE_FOR_ALL_COOLDOWN = timedelta(seconds=60)
# EDG-049: how long a combat turn waits for a holder who is not on the channel at all, before another
# seated player (not only the owner) may skip it. A present holder gets the full timeout.
COMBAT_ABSENT_GRACE = timedelta(seconds=MIN_TIMEOUT_SECONDS)

EVENT_TYPE = "turn.spotlight_changed"
_PUBLISH_TIMEOUT_SECONDS = 2.0
UNCHANGED: Any = object()


class SpotlightRefused(Exception):
    """A spotlight rule refused the action. ``code`` is a registered, value-free safe error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Member:
    """One character of the rotation, with who may act for it and whether it can take a turn."""

    character_id: int
    name: str
    controller_id: int
    seated: bool
    eligible: bool
    display_name: str | None = None


@dataclass
class Table:
    """A session's spotlight state as read under the current transaction."""

    session: SessionModel
    owner_id: int
    members: dict[int, Member]
    rotation: list[int]
    present: set[int]
    stored_order: list[int]

    @property
    def holder(self) -> int | None:
        return self.session.active_character_id

    def engaged(self) -> bool:
        return len(self.rotation) >= 2

    def needs_write(self) -> bool:
        return self.rotation != self.stored_order

    def seat_of(self, user_id: int) -> int | None:
        for cid in self.rotation:
            member = self.members[cid]
            if member.seated and member.controller_id == user_id:
                return cid
        return None


@dataclass(frozen=True)
class QueuedClaim:
    """A turn that plays the caller's queued action (EDG-051): validated again under the table lock."""

    action_id: str
    version: int | None
    message: str


@dataclass
class SpotlightTurn:
    """The spotlight decision for one turn, taken under the table lock before the model runs.

    ``consumes`` is False for the opening intro, which may claim or be refused but never passes.
    ``passed_reason`` is set by ``apply_turn_commit`` when the turn's commit moved the spotlight.
    """

    session_id: int
    version: int
    holder: int | None
    mode: str
    consumes: bool
    pass_to: int | None = None
    passed_reason: str | None = None
    # EDG-051: the queued action this turn plays, consumed by the turn's own commit.
    queued_user: int | None = None
    queued_id: str | None = None
    queued_version: int | None = None
    queue_consumed: bool = False
    # EDG-049: admitted as the combat turn holder's turn (or a skip of it).
    combat: bool = False


@dataclass(frozen=True)
class Change:
    """A committed change to announce. ``announce`` is False when neither the old nor the new table is engaged."""

    reason: str
    announce: bool


@dataclass(frozen=True)
class Opened:
    plan: SpotlightTurn | None
    change: Change | None


def _utc(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _eligible(table: Table, character_id: int | None) -> bool:
    member = table.members.get(character_id) if character_id is not None else None
    return member is not None and member.eligible


def _after(rotation: list[int], after: int | None) -> list[int]:
    """The rotation in turn order after ``after`` (excluded), wrapping around. All of it when ``after`` is absent."""
    if after is None or after not in rotation:
        return list(rotation)
    index = rotation.index(after)
    return rotation[index + 1 :] + rotation[:index]


def _pick(table: Table, after: int | None) -> int | None:
    """The next eligible member, in rotation order, who is present on the channel."""
    for character_id in _after(table.rotation, after):
        member = table.members[character_id]
        if member.eligible and member.controller_id in table.present:
            return character_id
    return None


def _cooldown_until(session: SessionModel, character_id: int) -> datetime | None:
    raw = (session.spotlight_cooldowns or {}).get(str(character_id))
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _prune_cooldowns(session: SessionModel, now: datetime) -> None:
    kept: dict[str, str] = {}
    for key, raw in (session.spotlight_cooldowns or {}).items():
        try:
            if datetime.fromisoformat(raw) > now:
                kept[key] = raw
        except (TypeError, ValueError):
            continue
    session.spotlight_cooldowns = kept


def _pass_due(session: SessionModel, table: Table, now: datetime) -> str | None:
    """Why the holder loses the spotlight now: 'timeout', 'auto_pass' (fast policy, holder left) or None."""
    if _timed_out(session, now):
        return "timeout"
    holder = table.members.get(session.active_character_id) if session.active_character_id else None
    since = session.spotlight_since
    if (
        pacing.auto_pass(session)
        and holder is not None
        and since is not None
        and holder.controller_id not in table.present
        and now - since >= pacing.AUTO_PASS_GRACE
    ):
        return "auto_pass"
    return None


def _timed_out(session: SessionModel, now: datetime) -> bool:
    since = session.spotlight_since
    if session.active_character_id is None or session.spotlight_mode == MODE_OWNER_DIRECTED or since is None:
        return False
    return now - since >= timedelta(seconds=session.spotlight_timeout_seconds)


async def running_party_combat(db: AsyncSession, session_id: int) -> dict | None:
    """The session's open fight when it has two or more heroes (EDG-049), else None."""
    combat = await load_active_combat(db, session_id)
    if combat is None or combat.get("ended") or not is_party(combat.get("combatants")):
        return None
    return combat


def combat_hero_ids(combat: dict | None) -> list[int]:
    return [
        int(row["character_id"])
        for row in (combat or {}).get("combatants", [])
        if row.get("is_player") and row.get("character_id") is not None
    ]


def combat_holder(table: Table, combat: dict) -> int | None:
    """The hero whose turn it is now: the next slot's hero, or the next hero after it who can still act.

    A hero who left, died, is down or whose seat can no longer act is passed over, so a fight never
    waits on a chair that cannot answer. None when nobody at the table can take the turn.
    """
    rows = combat.get("combatants") or []
    index = combat.get("current_turn_index")
    if not rows or type(index) is not int:
        return None
    for step in range(len(rows)):
        row = rows[(index + step) % len(rows)]
        if not row.get("is_player") or row.get("character_id") is None:
            continue
        if row.get("fled") or row.get("disposition") == "dead" or int(row.get("hp_current") or 0) <= 0:
            continue
        if _eligible(table, int(row["character_id"])):
            return int(row["character_id"])
    return None


def combat_due(session: SessionModel, table: Table, holder: int | None, now: datetime) -> bool:
    """Whether the holder's combat turn may be skipped by a seated player (the owner may always skip).

    Overdue: idle for the table's timeout (never in owner-directed mode). Absent: the holder's controller
    is not on the channel and a short grace has passed.
    """
    if holder is None or session.spotlight_since is None:
        return False
    if _timed_out(session, now):
        return True
    member = table.members.get(holder)
    absent = member is not None and member.controller_id not in table.present
    return (
        absent
        and session.spotlight_mode != MODE_OWNER_DIRECTED
        and now - session.spotlight_since >= COMBAT_ABSENT_GRACE
    )


async def _lock_session(db: AsyncSession, session_id: int) -> SessionModel:
    session = (
        await db.execute(
            select(SessionModel).where(SessionModel.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def load_table(
    db: AsyncSession,
    redis: Redis | None,
    session: SessionModel,
    *,
    now: datetime | None = None,
    present_extra: set[int] | None = None,
    include: list[int] | None = None,
) -> Table:
    """Read the rotation, eligibility and presence. Never writes; callers persist ``rotation`` if they change it.

    Rotation rule: a stored or candidate character stays when it is seated, or when the campaign
    owner controls it. Seats join in seat order. A holder that is neither seated nor owner-controlled
    (a removed member's hero) is therefore not in the rotation and is released by the caller.
    """
    campaign = await db.get(Campaign, session.campaign_id)
    owner_id = campaign.created_by if campaign is not None else 0
    seat_rows = (
        await db.execute(
            select(SessionParticipant.character_id, SessionParticipant.user_id, User.display_name)
            .join(User, User.id == SessionParticipant.user_id)
            .where(SessionParticipant.session_id == session.id)
            .order_by(SessionParticipant.id)
        )
    ).all()
    seated = {character_id: (user_id, name) for character_id, user_id, name in seat_rows}
    stored = [int(cid) for cid in (session.spotlight_order or []) if isinstance(cid, int)]
    candidates = list(dict.fromkeys(stored + list(include or []) + [session.active_character_id]))
    candidates = [cid for cid in candidates if cid is not None]
    wanted = sorted(set(candidates) | set(seated))
    rows = (
        await db.execute(
            select(Character.id, Character.name, Character.owner_user_id).where(Character.id.in_(wanted))
        )
    ).all() if wanted else []
    info = {row.id: row for row in rows}

    rotation: list[int] = []
    for character_id in candidates:
        row = info.get(character_id)
        if row is None or character_id in rotation:
            continue
        if character_id in seated or row.owner_user_id == owner_id:
            rotation.append(character_id)
    for character_id in [row[0] for row in seat_rows]:
        if character_id not in rotation and character_id in info:
            rotation.append(character_id)

    members: dict[int, Member] = {}
    for character_id in rotation:
        row = info[character_id]
        is_seated = character_id in seated
        controller = seated[character_id][0] if is_seated else owner_id
        state = await character_play_state(db, character_id, session.campaign_id)
        eligible = state is not None and life_state_of(state.get("stats")) != "dead"
        members[character_id] = Member(
            character_id=character_id,
            name=row.name,
            controller_id=controller,
            seated=is_seated,
            eligible=eligible,
        )
    controllers = sorted({m.controller_id for m in members.values()})
    names = dict(
        (
            await db.execute(select(User.id, User.display_name).where(User.id.in_(controllers)))
        ).all()
    ) if controllers else {}
    for member in members.values():
        member.display_name = names.get(member.controller_id)

    present: set[int] = set(present_extra or set())
    if redis is not None:
        roster = await session_channel.roster(redis, session.id, now=_utc(now).timestamp())
        present |= {entry.user_id for entry in roster}
    return Table(
        session=session,
        owner_id=owner_id,
        members=members,
        rotation=rotation,
        present=present,
        stored_order=stored,
    )


def _apply_rotation(session: SessionModel, table: Table) -> None:
    if table.needs_write():
        session.spotlight_order = list(table.rotation)


async def open_turn(
    db: AsyncSession,
    redis: Redis,
    session_id: int,
    auth: Any,
    *,
    expected_version: int | None,
    consumes: bool,
    now: datetime | None = None,
    queued: QueuedClaim | None = None,
) -> Opened:
    """Decide who may act now, under the table lock. Commits its own transaction.

    Raises ``SpotlightRefused`` (after rollback) when the caller may not act; nothing has been spent
    by then. The caller must hold the turn lock for ``session_id``.
    """
    if auth is None:
        if queued is not None:
            raise SpotlightRefused("queue_unavailable")
        # Local single-user mode has no accounts to arbitrate between: no lock, no read, no change.
        return Opened(plan=None, change=None)
    t = _utc(now)
    session = await _lock_session(db, session_id)
    try:
        return await _open_turn(db, redis, session, auth.user_id, expected_version, consumes, t, queued)
    except SpotlightRefused:
        await db.rollback()
        raise


async def _open_turn(
    db: AsyncSession,
    redis: Redis,
    session: SessionModel,
    caller: int,
    expected_version: int | None,
    consumes: bool,
    now: datetime,
    queued: QueuedClaim | None = None,
) -> Opened:
    combat = await running_party_combat(db, session.id)
    table = await load_table(
        db, redis, session, now=now, present_extra={caller}, include=combat_hero_ids(combat)
    )
    if not table.engaged():
        if queued is not None:
            raise SpotlightRefused("queue_unavailable")
        _apply_rotation(session, table)
        await db.commit()
        return Opened(plan=None, change=None)
    if expected_version is not None and expected_version != session.spotlight_version:
        raise SpotlightRefused("spotlight_stale")
    if combat is not None:
        return await _open_combat_turn(db, session, table, combat, caller, consumes, now)

    mode = session.spotlight_mode
    version = session.spotlight_version
    old_holder = table.holder
    holder = old_holder
    reason: str | None = None

    # A holder who is idle past the timeout (or, on the fast policy, has left the table) loses the
    # spotlight unless the holder is the one acting now.
    due = _pass_due(session, table, now) if holder is not None else None
    if (
        due is not None
        and table.members.get(holder) is not None
        and table.members[holder].controller_id != caller
    ):
        target = _pick(table, holder)
        if target is not None:
            holder, reason = target, due
        elif mode == MODE_FREE_FOR_ALL:
            holder, reason = None, due

    if holder is not None and not _eligible(table, holder):
        holder, reason = None, reason or "released"
    if holder is None and mode == MODE_ROUND_ROBIN:
        target = _pick(table, None)
        if target is not None:
            holder, reason = target, reason or "assigned"

    if holder is not None:
        if table.members[holder].controller_id != caller:
            raise SpotlightRefused("not_your_spotlight")
    else:
        seat = table.seat_of(caller)
        if mode != MODE_FREE_FOR_ALL or seat is None or not _eligible(table, seat):
            raise SpotlightRefused("spotlight_unavailable")
        until = _cooldown_until(session, seat)
        if until is not None and now < until:
            raise SpotlightRefused("spotlight_cooldown")
        holder, reason = seat, "claimed"

    queued_user: int | None = None
    if queued is not None:
        # The caller holds the spotlight now. Validate the queued action against the current
        # situation; a refusal rolls everything back, so nothing is spent and nothing is consumed.
        current = await pacing.basis_of(db, session, holder)
        code = pacing.check_claim(
            pacing.get_item(session, caller),
            action_id=queued.action_id,
            version=queued.version,
            message=queued.message,
            holder=holder,
            current=current,
            now=now,
        )
        if code is not None:
            raise SpotlightRefused(code)
        queued_user = caller

    if holder != old_holder:
        version += 1
    # Whoever holds the spotlight now is acting now (a claim, a hand-over or the holder's own turn),
    # so the idle clock restarts.
    session.spotlight_since = now
    session.active_character_id = holder
    session.spotlight_version = version
    _apply_rotation(session, table)
    _prune_cooldowns(session, now)

    pass_to = _pick(table, holder) if consumes and mode == MODE_ROUND_ROBIN else None
    plan = SpotlightTurn(
        session_id=session.id,
        version=version,
        holder=holder,
        mode=mode,
        consumes=consumes,
        pass_to=pass_to,
        queued_user=queued_user,
        queued_id=queued.action_id if queued is not None else None,
        queued_version=queued.version if queued is not None else None,
    )
    await db.commit()
    change = Change(reason=reason, announce=True) if reason is not None else None
    return Opened(plan=plan, change=change)


async def _open_combat_turn(
    db: AsyncSession,
    session: SessionModel,
    table: Table,
    combat: dict,
    caller: int,
    consumes: bool,
    now: datetime,
) -> Opened:
    """Admit the combat turn holder's own turn (EDG-049). Anyone else is refused before any model call."""
    holder = combat_holder(table, combat)
    if holder is None:
        raise SpotlightRefused("spotlight_unavailable")
    if table.members[holder].controller_id != caller:
        raise SpotlightRefused("not_your_spotlight")
    version = session.spotlight_version
    reason: str | None = None
    if holder != table.holder:
        version += 1
        reason = "combat_turn"
    session.spotlight_since = now
    session.active_character_id = holder
    session.spotlight_version = version
    _apply_rotation(session, table)
    mode = session.spotlight_mode
    plan = SpotlightTurn(
        session_id=session.id,
        version=version,
        holder=holder,
        mode=mode,
        consumes=consumes,
        pass_to=_pick(table, holder) if consumes and mode == MODE_ROUND_ROBIN else None,
        combat=True,
    )
    await db.commit()
    return Opened(plan=plan, change=Change(reason=reason, announce=True) if reason else None)


async def open_skip(
    db: AsyncSession,
    redis: Redis,
    session_id: int,
    auth: Any,
    *,
    expected_version: int | None,
    now: datetime | None = None,
) -> Opened:
    """Admit the skip of the combat turn holder's idle turn (EDG-049). Commits its own transaction.

    The owner may skip any turn; a seated player only when the holder is overdue or absent (`combat_due`).
    The holder is the hero who takes the default Dodge. Raises ``SpotlightRefused`` after rollback.
    The caller must hold the turn lock.
    """
    t = _utc(now)
    session = await _lock_session(db, session_id)
    try:
        combat = await running_party_combat(db, session.id)
        if combat is None:
            raise SpotlightRefused("no_party_combat")
        table = await load_table(
            db, redis, session, now=t, present_extra={auth.user_id}, include=combat_hero_ids(combat)
        )
        if expected_version is not None and expected_version != session.spotlight_version:
            raise SpotlightRefused("spotlight_stale")
        holder = combat_holder(table, combat)
        if holder is None:
            raise SpotlightRefused("spotlight_unavailable")
        is_owner = auth.user_id == table.owner_id
        if not is_owner and table.seat_of(auth.user_id) is None:
            raise SpotlightRefused("not_your_spotlight")
        if not is_owner and not combat_due(session, table, holder, t):
            raise SpotlightRefused("combat_skip_not_due")
        version = session.spotlight_version
        if holder != table.holder:
            version += 1
        session.active_character_id = holder
        session.spotlight_since = t
        session.spotlight_version = version
        _apply_rotation(session, table)
        mode = session.spotlight_mode
        plan = SpotlightTurn(
            session_id=session.id,
            version=version,
            holder=holder,
            mode=mode,
            consumes=True,
            pass_to=_pick(table, holder) if mode == MODE_ROUND_ROBIN else None,
            combat=True,
        )
        await db.commit()
        return Opened(plan=plan, change=None)
    except SpotlightRefused:
        await db.rollback()
        raise


def apply_turn_commit(
    session: SessionModel,
    plan: SpotlightTurn | None,
    *,
    now: datetime,
    combat_holder: int | None = None,
) -> str | None:
    """Pass the spotlight when a turn commits. Called inside the turn's transaction, on the locked row.

    Raises ``SpotlightRefused("spotlight_stale")`` if the spotlight moved after the turn was admitted;
    the caller rolls back, so the turn is not committed. Returns the reason when the spotlight moved.
    ``combat_holder`` (EDG-049) is the hero whose slot is next in a fight this turn left running.
    """
    if plan is None or not plan.consumes:
        return None
    if session.spotlight_version != plan.version or session.active_character_id != plan.holder:
        raise SpotlightRefused("spotlight_stale")
    if plan.queued_user is not None and plan.queued_id is not None:
        outcome = pacing.consume(session, plan.queued_user, plan.queued_id, plan.queued_version)
        if outcome == "gone":
            # Played by another turn or cancelled while this one ran: never commit it a second time.
            raise SpotlightRefused("queued_action_missing")
        plan.queue_consumed = outcome == "consumed"
    if combat_holder is not None:
        # EDG-049: a running party fight passes the turn along its initiative order, whatever the
        # table's rotation rule is. The same hero may follow itself when the others cannot act.
        session.active_character_id = combat_holder
        session.spotlight_version = plan.version + 1
        session.spotlight_since = now
        plan.passed_reason = "combat_turn_passed" if combat_holder != plan.holder else "combat_turn_continues"
        return plan.passed_reason
    if plan.mode == MODE_ROUND_ROBIN:
        if plan.pass_to is None or plan.pass_to == plan.holder:
            return None
        session.active_character_id = plan.pass_to
        reason = "turn_passed"
    elif plan.mode == MODE_FREE_FOR_ALL and plan.holder is not None:
        cooldowns = dict(session.spotlight_cooldowns or {})
        cooldowns[str(plan.holder)] = (now + FREE_FOR_ALL_COOLDOWN).isoformat()
        session.spotlight_cooldowns = cooldowns
        session.active_character_id = None
        reason = "turn_released"
    else:
        return None
    session.spotlight_version = plan.version + 1
    session.spotlight_since = now
    _prune_cooldowns(session, now)
    plan.passed_reason = reason
    return reason


async def precheck(db: AsyncSession, session_id: int, auth: Any, *, now: datetime | None = None) -> None:
    """Read-only early refusal before admission or the lock: a holder who is definitely someone else.

    It never mutates and never spends. Anything it cannot decide (free spotlight, timed-out holder,
    unengaged table) is left to ``open_turn``, which is authoritative.
    """
    if auth is None:
        return
    session = await db.get(SessionModel, session_id)
    if session is None or session.active_character_id is None:
        return
    table = await load_table(db, None, session, now=_utc(now))
    if not table.engaged() or _timed_out(session, _utc(now)) or pacing.auto_pass(session):
        # An auto-pass policy depends on presence, which only the locked open_turn reads.
        return
    member = table.members.get(session.active_character_id)
    if member is not None and member.eligible and member.controller_id != auth.user_id:
        raise SpotlightRefused("not_your_spotlight")


async def host_update(
    db: AsyncSession,
    redis: Redis,
    session_id: int,
    *,
    owner_id: int,
    mode: str | None = None,
    timeout_seconds: int | None = None,
    holder: Any = UNCHANGED,
    expected_version: int | None = None,
    now: datetime | None = None,
    pacing_policy: str | None = None,
) -> Change | None:
    """Owner change: mode, timeout, or hand the spotlight to a hero (``None`` releases it). Caller holds the lock."""
    t = _utc(now)
    session = await _lock_session(db, session_id)
    try:
        return await _host_update(
            db, redis, session, owner_id, mode, timeout_seconds, holder, expected_version, t, pacing_policy
        )
    except SpotlightRefused:
        await db.rollback()
        raise


async def _host_update(
    db: AsyncSession,
    redis: Redis,
    session: SessionModel,
    owner_id: int,
    mode: str | None,
    timeout_seconds: int | None,
    holder: Any,
    expected_version: int | None,
    now: datetime,
    pacing_policy: str | None = None,
) -> Change | None:
    if expected_version is not None and expected_version != session.spotlight_version:
        raise SpotlightRefused("spotlight_stale")
    if holder is not UNCHANGED and await running_party_combat(db, session.id) is not None:
        # EDG-049: the initiative order decides the holder while a shared fight is running.
        raise SpotlightRefused("combat_turn_locked")
    old_order = list(session.spotlight_order or [])
    was_engaged = len(old_order) >= 2
    old_holder = session.active_character_id
    old_mode = session.spotlight_mode
    old_timeout = session.spotlight_timeout_seconds

    include: list[int] = []
    if holder is not UNCHANGED and holder is not None:
        character = await db.get(Character, holder)
        assigned = await character_play_state(db, holder, session.campaign_id)
        seated = await db.scalar(
            select(SessionParticipant.id).where(
                SessionParticipant.session_id == session.id,
                SessionParticipant.character_id == holder,
            )
        )
        if character is None or assigned is None or (seated is None and character.owner_user_id != owner_id):
            raise SpotlightRefused("spotlight_holder_invalid")
        include = [holder]
    table = await load_table(db, redis, session, now=now, present_extra={owner_id}, include=include)

    new_holder = old_holder
    if holder is not UNCHANGED:
        if holder is None:
            new_holder = None
        elif not _eligible(table, holder):
            raise SpotlightRefused("spotlight_holder_invalid")
        else:
            new_holder = holder
    if mode is not None:
        session.spotlight_mode = mode
    old_policy = session.pacing_policy
    if pacing_policy is not None:
        session.pacing_policy = pacing_policy
        session.spotlight_timeout_seconds = pacing.preset(pacing_policy).timeout_seconds
    elif timeout_seconds is not None and timeout_seconds != old_timeout:
        # An explicit timeout is no longer any named policy.
        session.pacing_policy = pacing.POLICY_CUSTOM
    if timeout_seconds is not None and pacing_policy is None:
        session.spotlight_timeout_seconds = timeout_seconds
    current_mode = session.spotlight_mode

    reason: str | None = None
    if new_holder != old_holder:
        reason = "host_set" if new_holder is not None else "host_released"
    if new_holder is None and current_mode == MODE_ROUND_ROBIN:
        target = _pick(table, None)
        if target is not None:
            new_holder = target
    if mode is not None and mode != old_mode and reason is None:
        reason = "mode_changed"
    if pacing_policy is not None and session.pacing_policy != old_policy and reason is None:
        reason = "pacing_changed"
    if timeout_seconds is not None and timeout_seconds != old_timeout and reason is None:
        reason = "settings_changed"
    if new_holder != old_holder and reason is None:
        reason = "assigned"

    rotation_changed = table.rotation != old_order
    if reason is None and not rotation_changed:
        await db.commit()
        return None

    if new_holder != old_holder:
        session.spotlight_since = now if new_holder is not None else None
    session.active_character_id = new_holder
    session.spotlight_version = session.spotlight_version + 1
    _apply_rotation(session, table)
    await db.commit()
    return Change(reason=reason or "settings_changed", announce=was_engaged or table.engaged())


async def on_seat_change(
    db: AsyncSession,
    redis: Redis,
    session_id: int,
    *,
    reason: str,
    now: datetime | None = None,
    actor_id: int | None = None,
) -> Change | None:
    """Re-settle the table after seats changed in this transaction. Caller holds the lock and commits.

    Releases a spotlight held by a seat that is gone or can no longer act, hands a free round-robin
    spotlight to the next present member, and persists the new rotation. The caller commits, so the
    seat change and the membership change it belongs to land together or not at all.
    """
    t = _utc(now)
    session = await _lock_session(db, session_id)
    old_order = list(session.spotlight_order or [])
    old_holder = session.active_character_id
    # The member making the change is present by definition (they are at the table right now).
    table = await load_table(
        db, redis, session, now=t, present_extra={actor_id} if actor_id is not None else None
    )
    holder = old_holder
    combat = await running_party_combat(db, session.id)
    if combat is not None:
        # EDG-049: a fight keeps its own order; a seat that left is passed over, not replaced.
        holder = combat_holder(table, combat)
    else:
        if holder is not None and not _eligible(table, holder):
            holder = None
        if holder is None and session.spotlight_mode == MODE_ROUND_ROBIN:
            target = _pick(table, None)
            if target is not None:
                holder = target
    # A player who lost the seat (or the character) loses the queued action that went with it.
    dropped = pacing.prune_unseated(
        session, {cid: member.controller_id for cid, member in table.members.items()}
    )
    if holder == old_holder and table.rotation == old_order:
        if dropped:
            return Change(reason="queue_changed", announce=table.engaged())
        return None
    if holder != old_holder:
        session.spotlight_since = t if holder is not None else None
    session.active_character_id = holder
    session.spotlight_version = session.spotlight_version + 1
    _apply_rotation(session, table)
    return Change(reason=reason, announce=len(old_order) >= 2 or table.engaged())


async def sessions_with_seats(db: AsyncSession, campaign_id: int, user_id: int) -> list[int]:
    """Session ids in the campaign where this user holds a seat (the tables a removal must lock)."""
    rows = await db.execute(
        select(SessionParticipant.session_id)
        .join(SessionModel, SessionModel.id == SessionParticipant.session_id)
        .where(SessionModel.campaign_id == campaign_id, SessionParticipant.user_id == user_id)
        .order_by(SessionParticipant.session_id)
    )
    return list(dict.fromkeys(rows.scalars().all()))


async def is_engaged(db: AsyncSession, session_id: int) -> bool:
    """Whether a session's table is multi-PC, read without locks (the owner's handover path uses it)."""
    session = await db.get(SessionModel, session_id)
    if session is None:
        return False
    table = await load_table(db, None, session)
    return table.engaged()


async def public_view(
    db: AsyncSession,
    redis: Redis,
    session_id: int,
    *,
    now: datetime | None = None,
    viewer_user_id: int | None = None,
) -> dict[str, Any]:
    """The table's public spotlight: holder, rule, version, next-up order. Read-only."""
    t = _utc(now)
    session = await db.get(SessionModel, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    combat_now = await running_party_combat(db, session_id)
    table = await load_table(db, redis, session, now=t, include=combat_hero_ids(combat_now))
    queue = await _queue_view(db, session, table, t, viewer_user_id)
    queued_users = {item["user_id"] for item in queue["items"] if item["status"] != "expired"}
    holder_view = None
    holder = table.holder
    if holder is not None and holder in table.members:
        member = table.members[holder]
        holder_view = _member_view(member, table, eligible=member.eligible)
    next_up = []
    for character_id in _after(table.rotation, holder):
        member = table.members[character_id]
        if member.eligible:
            view = _member_view(member, table)
            view["queued"] = member.controller_id in queued_users
            next_up.append(view)
    combat_view = None
    combat = combat_now
    if combat is not None:
        effective = combat_holder(table, combat)
        combat_view = {
            "round": combat.get("round"),
            "holder_character_id": effective,
            "skip_due": combat_due(session, table, effective, t),
        }
    deadline = None
    if (
        holder is not None
        and session.spotlight_since is not None
        and session.spotlight_mode != MODE_OWNER_DIRECTED
    ):
        deadline = session.spotlight_since + timedelta(seconds=session.spotlight_timeout_seconds)
    return {
        "session_id": session.id,
        "combat": combat_view,
        "engaged": table.engaged(),
        "mode": session.spotlight_mode,
        "version": session.spotlight_version,
        "timeout_seconds": session.spotlight_timeout_seconds,
        "holder": holder_view,
        "since": session.spotlight_since.isoformat() if session.spotlight_since is not None else None,
        "handover_due": _pass_due(session, table, t) is not None or bool(combat_view and combat_view["skip_due"]),
        # When an idle holder loses the spotlight (null when it never times out or nobody holds it).
        "turn_deadline": deadline.isoformat() if deadline is not None else None,
        "next_up": next_up,
        "pacing": pacing.pacing_view(session),
        "queue": queue,
    }


async def _queue_view(
    db: AsyncSession, session: SessionModel, table: Table, now: datetime, viewer_user_id: int | None
) -> dict[str, Any]:
    items = (session.queued_actions or {}).get("items") if isinstance(session.queued_actions, dict) else None
    current: dict[int, dict[str, Any]] = {}
    for item in (items or {}).values():
        character_id = item.get("character_id")
        if isinstance(character_id, int) and character_id not in current:
            current[character_id] = await pacing.basis_of(db, session, character_id)
    return pacing.queue_view(session, table.members, current, now, viewer_user_id=viewer_user_id)


def _member_view(member: Member, table: Table, *, eligible: bool | None = None) -> dict[str, Any]:
    view: dict[str, Any] = {
        "character_id": member.character_id,
        "character_name": member.name,
        "user_id": member.controller_id,
        "display_name": member.display_name,
        "present": member.controller_id in table.present,
    }
    if eligible is not None:
        view["eligible"] = eligible
    return view


async def announce(
    redis: Redis,
    session_id: int,
    change: Change,
    *,
    actor: ChannelActor | None = None,
    turn_id: str | None = None,
) -> None:
    """Publish ``turn.spotlight_changed`` with the table's state as committed. Best effort; never raises.

    Call while the table lock is still held, after the change committed, so events stay in version order.
    """
    try:
        async with db_session_module.async_session_factory() as db:
            view = await public_view(db, redis, session_id)
        view["reason"] = change.reason
        await asyncio.wait_for(
            session_channel.publish(
                redis,
                session_id,
                kind=EVENT_TYPE,
                actor=actor,
                turn_id=turn_id,
                data=view,
            ),
            timeout=_PUBLISH_TIMEOUT_SECONDS,
        )
    except Exception:
        log_failure(
            "session_spotlight_publish_failed",
            classify_error("internal_fault"),
            level="warning",
            session_id=session_id,
        )


__all__ = [
    "DEFAULT_MODE",
    "DEFAULT_TIMEOUT_SECONDS",
    "EVENT_TYPE",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "MODES",
    "MODE_FREE_FOR_ALL",
    "MODE_OWNER_DIRECTED",
    "MODE_ROUND_ROBIN",
    "COMBAT_ABSENT_GRACE",
    "Change",
    "QueuedClaim",
    "SpotlightRefused",
    "SpotlightTurn",
    "UNCHANGED",
    "announce",
    "apply_turn_commit",
    "host_update",
    "is_engaged",
    "on_seat_change",
    "open_skip",
    "open_turn",
    "precheck",
    "running_party_combat",
    "public_view",
    "sessions_with_seats",
]
