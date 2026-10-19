"""Campaign membership invites and the session participant roster (EDG-046).

Invites are created and listed by the campaign owner only. Accepting one requires an authenticated
caller (token mode); the accepting user must own the character they seat. Token-level failures
(unknown, expired, revoked, already used, wrong invited account) share one generic 404 so the
response never reveals which condition applied.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import (
    Campaign,
    CampaignInvite,
    CampaignMembership,
    Character,
    Session as SessionModel,
    SessionParticipant,
    User,
)

from ..auth import (
    OWNER_ROLE,
    PLAYER_ROLE,
    Auth,
    campaign_role,
    generate_invite_token,
    hash_token,
    member_campaign,
    member_session,
    owned_campaign,
)
from ..dependencies import acquire_turn_lock, current_user_id, get_redis, get_session
from ..schemas.party import (
    InviteAccept,
    InviteAccepted,
    InviteCreate,
    InviteCreated,
    InviteRead,
    LobbyMember,
    LobbyRead,
    LobbyViewer,
    MemberRead,
    MemberSeat,
    MemberUpdate,
    SessionParticipantRead,
)
from ..services import spotlight
from ..services.character_assignments import load_active_assignment
from ..services.session_broadcast import notify_access_changed

router = APIRouter(tags=["campaign-membership"])


def _invite_status(invite: CampaignInvite, now: datetime) -> str:
    if invite.revoked_at is not None:
        return "revoked"
    if invite.accepted_at is not None:
        return "accepted"
    if invite.expires_at <= now:
        return "expired"
    return "active"


def _invite_read(invite: CampaignInvite, now: datetime) -> InviteRead:
    return InviteRead(
        id=invite.id,
        campaign_id=invite.campaign_id,
        session_id=invite.session_id,
        role=invite.role,
        invited_email=invite.invited_email,
        status=_invite_status(invite, now),
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        accepted_at=invite.accepted_at,
        revoked_at=invite.revoked_at,
    )


def _invalid_invite() -> HTTPException:
    return HTTPException(status_code=404, detail="Invite not found or no longer valid.")


@router.post(
    "/campaigns/{campaign_id}/invites",
    response_model=InviteCreated,
    status_code=201,
)
async def create_invite(
    campaign_id: int,
    body: InviteCreate,
    auth: Auth,
    owner_id: int = Depends(current_user_id),
    db: AsyncSession = Depends(get_session),
):
    campaign = await owned_campaign(db, campaign_id, auth)
    if body.session_id is not None:
        session = await db.get(SessionModel, body.session_id)
        if session is None or session.campaign_id != campaign.id:
            raise HTTPException(status_code=404, detail="Session not found")
    now = datetime.now(UTC)
    token = generate_invite_token()
    invite = CampaignInvite(
        campaign_id=campaign.id,
        session_id=body.session_id,
        created_by=owner_id,
        token_hash=hash_token(token),
        role=body.role,
        invited_email=body.invited_email.strip().casefold() if body.invited_email else None,
        expires_at=now + timedelta(hours=body.expires_in_hours),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    return InviteCreated(**_invite_read(invite, now).model_dump(), token=token)


@router.get("/campaigns/{campaign_id}/invites", response_model=list[InviteRead])
async def list_invites(
    campaign_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
):
    campaign = await owned_campaign(db, campaign_id, auth)
    rows = (
        await db.execute(
            select(CampaignInvite)
            .where(CampaignInvite.campaign_id == campaign.id)
            .order_by(CampaignInvite.id)
        )
    ).scalars()
    now = datetime.now(UTC)
    return [_invite_read(invite, now) for invite in rows]


@router.delete("/campaigns/{campaign_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(
    campaign_id: int,
    invite_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
):
    campaign = await owned_campaign(db, campaign_id, auth)
    invite = (
        await db.execute(
            select(CampaignInvite).where(
                CampaignInvite.id == invite_id, CampaignInvite.campaign_id == campaign.id
            )
        )
    ).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.accepted_at is not None:
        raise HTTPException(status_code=409, detail="Invite was already accepted.")
    if invite.revoked_at is None:
        invite.revoked_at = datetime.now(UTC)
        await db.commit()


@router.post("/invites/accept", response_model=InviteAccepted)
async def accept_invite(
    body: InviteAccept,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    if auth is None:
        # Local single-user mode has no accounts to join with; acceptance needs an authenticated user.
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    now = datetime.now(UTC)
    invite = (
        await db.execute(
            select(CampaignInvite).where(CampaignInvite.token_hash == hash_token(body.token))
        )
    ).scalar_one_or_none()
    if invite is None or invite.revoked_at is not None or invite.accepted_at is not None:
        raise _invalid_invite()
    if invite.expires_at <= now:
        raise _invalid_invite()
    user = await db.get(User, auth.user_id)
    if user is None:
        raise _invalid_invite()
    if invite.invited_email is not None and user.email.casefold() != invite.invited_email:
        raise _invalid_invite()
    campaign = await db.get(Campaign, invite.campaign_id)
    if campaign is None:
        raise _invalid_invite()

    seated_character_id: int | None = None
    if invite.session_id is not None and invite.role == PLAYER_ROLE:
        if body.character_id is None:
            raise HTTPException(
                status_code=422,
                detail="Choose the character to seat at this session.",
            )
        # A character the caller does not own is reported exactly like a missing one.
        character = await db.get(Character, body.character_id)
        if character is None or character.owner_user_id != auth.user_id:
            raise HTTPException(status_code=404, detail="Character not found")
        if await load_active_assignment(db, character.id, invite.campaign_id) is None:
            raise HTTPException(
                status_code=422,
                detail="Character is not assigned to this campaign.",
            )
        seats = (
            await db.execute(
                select(SessionParticipant.user_id, SessionParticipant.character_id).where(
                    SessionParticipant.session_id == invite.session_id,
                )
            )
        ).all()
        if any(user_id == auth.user_id for user_id, _ in seats):
            raise HTTPException(
                status_code=409,
                detail="You already have a character seated at this session.",
            )
        if any(char_id == character.id for _, char_id in seats):
            raise HTTPException(
                status_code=409,
                detail="That character is already seated at this session.",
            )
        seated_character_id = character.id

    # Consume the invite in one conditional UPDATE, so two concurrent accepts cannot both win. The
    # consume comes before the table lock: a racing accept waits on this row and then gets the same
    # generic refusal as any used invite, never a different answer that would reveal the race.
    consumed = (
        await db.execute(
            update(CampaignInvite)
            .where(
                CampaignInvite.id == invite.id,
                CampaignInvite.accepted_at.is_(None),
                CampaignInvite.revoked_at.is_(None),
                CampaignInvite.expires_at > now,
            )
            .values(accepted_at=now, accepted_by_user_id=auth.user_id)
            .returning(CampaignInvite.id)
        )
    ).scalar_one_or_none()
    if consumed is None:
        await db.rollback()
        raise _invalid_invite()

    # EDG-048: a seat changes the table's rotation, so it is taken under the table lock. A busy table
    # refuses the accept and rolls the consume back, so the invite stays usable.
    try:
        locks = await _lock_tables(redis, [invite.session_id] if seated_character_id is not None else [])
    except HTTPException:
        await db.rollback()
        raise
    change = None
    try:
        try:
            if campaign.created_by != auth.user_id:
                await db.execute(
                    pg_insert(CampaignMembership)
                    .values(campaign_id=campaign.id, user_id=auth.user_id, role=invite.role)
                    .on_conflict_do_nothing(constraint="uq_campaign_membership_user")
                )
            if seated_character_id is not None:
                db.add(
                    SessionParticipant(
                        session_id=invite.session_id,
                        user_id=auth.user_id,
                        character_id=seated_character_id,
                        role=PLAYER_ROLE,
                    )
                )
                await db.flush()
                change = await spotlight.on_seat_change(
                    db, redis, invite.session_id, reason="seat_joined", actor_id=auth.user_id
                )
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(status_code=409, detail="The invite could not be applied.") from exc
    finally:
        for lock in locks:
            await lock.release()
    if change is not None and change.announce:
        await spotlight.announce(redis, invite.session_id, change)
    return InviteAccepted(
        campaign_id=campaign.id,
        role=invite.role,
        session_id=invite.session_id,
        character_id=seated_character_id,
    )


@router.get(
    "/sessions/{session_id}/participants",
    response_model=list[SessionParticipantRead],
)
async def list_session_participants(
    session_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
):
    await member_session(db, session_id, auth)
    rows = (
        await db.execute(
            select(SessionParticipant, Character.name, User.display_name)
            .join(Character, Character.id == SessionParticipant.character_id)
            .join(User, User.id == SessionParticipant.user_id)
            .where(SessionParticipant.session_id == session_id)
            .order_by(SessionParticipant.id)
        )
    ).all()
    return [
        SessionParticipantRead(
            user_id=seat.user_id,
            display_name=display_name,
            character_id=seat.character_id,
            character_name=character_name,
            role=seat.role,
            joined_at=seat.joined_at,
        )
        for seat, character_name, display_name in rows
    ]


@router.get("/sessions/{session_id}/lobby", response_model=LobbyRead)
async def get_session_lobby(
    session_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
):
    """Any member's view of the table: who is in the campaign, their roles and seats here (EDG-050).

    Display names and seated heroes only, never an email or an invite. Owners get the full
    management view from ``/members``; this is the read every role, spectators included, may use.
    """
    session = await member_session(db, session_id, auth)
    campaign = await db.get(Campaign, session.campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Session not found")
    memberships = (
        await db.execute(
            select(CampaignMembership.user_id, CampaignMembership.role)
            .where(CampaignMembership.campaign_id == campaign.id)
            .order_by(CampaignMembership.id)
        )
    ).all()
    roles: dict[int, str] = {campaign.created_by: OWNER_ROLE}
    for member_id, role in memberships:
        roles.setdefault(member_id, role)
    names = dict(
        (
            await db.execute(select(User.id, User.display_name).where(User.id.in_(list(roles))))
        ).all()
    )
    seats = {
        user_id: (character_id, character_name)
        for user_id, character_id, character_name in (
            await db.execute(
                select(SessionParticipant.user_id, SessionParticipant.character_id, Character.name)
                .join(Character, Character.id == SessionParticipant.character_id)
                .where(SessionParticipant.session_id == session.id)
            )
        ).all()
    }
    members = [
        LobbyMember(
            user_id=member_id,
            display_name=names.get(member_id),
            role=role,
            character_id=seats.get(member_id, (None, None))[0],
            character_name=seats.get(member_id, (None, None))[1],
        )
        for member_id, role in roles.items()
    ]
    if auth is None:
        viewer = LobbyViewer(user_id=None, role=OWNER_ROLE, character_id=None, character_name=None)
    else:
        own = seats.get(auth.user_id, (None, None))
        viewer = LobbyViewer(
            user_id=auth.user_id,
            role=await campaign_role(db, campaign, auth.user_id) or OWNER_ROLE,
            character_id=own[0],
            character_name=own[1],
        )
    return LobbyRead(session_id=session.id, campaign_id=campaign.id, viewer=viewer, members=members)


# Member lifecycle (EDG-046). Removal is immediate: every member_* helper and turn_session reads the
# membership and seat rows on each request, so there is no cache to invalidate. A removed member's
# seats are deleted (their characters are never touched). Outstanding invites are left for the owner
# to revoke; an accepted invite stays consumed, so re-admission needs a newly issued invite.


async def _membership_or_404(db, campaign_id: int, user_id: int) -> CampaignMembership:
    row = (
        await db.execute(
            select(CampaignMembership).where(
                CampaignMembership.campaign_id == campaign_id,
                CampaignMembership.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return row


async def _drop_seats(db, campaign_id: int, user_id: int) -> None:
    """Free every seat the user holds in this campaign's sessions. Characters are not deleted."""
    campaign_sessions = select(SessionModel.id).where(SessionModel.campaign_id == campaign_id)
    await db.execute(
        delete(SessionParticipant).where(
            SessionParticipant.user_id == user_id,
            SessionParticipant.session_id.in_(campaign_sessions),
        )
    )


async def _lock_tables(redis: Redis, session_ids: list[int]) -> list:
    """Take the turn lock of each table, in id order. A busy table refuses the change; nothing is held then."""
    locks = []
    for session_id in sorted(set(session_ids)):
        lock = await acquire_turn_lock(session_id, redis)
        if lock is None:
            for held in locks:
                await held.release()
            raise HTTPException(
                status_code=409,
                detail="A turn is in progress at this table. Please retry once it finishes.",
            )
        locks.append(lock)
    return locks


async def _release_seats(
    db,
    redis: Redis,
    campaign_id: int,
    user_id: int,
    session_ids: list[int],
    actor_id: int | None,
) -> list[tuple[int, spotlight.Change]]:
    """Free a member's seats, then settle each affected table's spotlight, in the caller's transaction.

    The caller holds the table locks for ``session_ids`` (see ``_lock_tables``) and commits. Returns
    the changes to announce once that commit has landed.
    """
    await _drop_seats(db, campaign_id, user_id)
    changes: list[tuple[int, spotlight.Change]] = []
    for session_id in session_ids:
        change = await spotlight.on_seat_change(
            db, redis, session_id, reason="seat_released", actor_id=actor_id
        )
        if change is not None and change.announce:
            changes.append((session_id, change))
    return changes


async def _announce_changes(redis: Redis, changes: list[tuple[int, spotlight.Change]]) -> None:
    for session_id, change in changes:
        await spotlight.announce(redis, session_id, change)


async def _announce_access(db, redis: Redis, campaign_id: int) -> None:
    """After a membership or seat change commits, wake every open subscriber of the campaign's sessions."""
    session_ids = (
        await db.execute(select(SessionModel.id).where(SessionModel.campaign_id == campaign_id))
    ).scalars().all()
    await notify_access_changed(redis, list(session_ids))


async def _member_read(db, membership: CampaignMembership) -> MemberRead:
    user = await db.get(User, membership.user_id)
    seats = (
        await db.execute(
            select(SessionParticipant.session_id, SessionParticipant.character_id, Character.name)
            .join(Character, Character.id == SessionParticipant.character_id)
            .join(SessionModel, SessionModel.id == SessionParticipant.session_id)
            .where(
                SessionModel.campaign_id == membership.campaign_id,
                SessionParticipant.user_id == membership.user_id,
            )
            .order_by(SessionParticipant.id)
        )
    ).all()
    return MemberRead(
        user_id=membership.user_id,
        display_name=user.display_name if user is not None else None,
        role=membership.role,
        joined_at=membership.joined_at,
        seats=[
            MemberSeat(session_id=sid, character_id=cid, character_name=name)
            for sid, cid, name in seats
        ],
    )


@router.get("/campaigns/{campaign_id}/members", response_model=list[MemberRead])
async def list_members(
    campaign_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
):
    """Owner view of the roster: roles and seats. Display names only, never emails."""
    campaign = await owned_campaign(db, campaign_id, auth)
    rows = (
        await db.execute(
            select(CampaignMembership)
            .where(CampaignMembership.campaign_id == campaign.id)
            .order_by(CampaignMembership.id)
        )
    ).scalars()
    return [await _member_read(db, row) for row in rows]


@router.patch("/campaigns/{campaign_id}/members/{user_id}", response_model=MemberRead)
async def change_member_role(
    campaign_id: int,
    user_id: int,
    body: MemberUpdate,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Owner changes a member between player and spectator.

    Demoting to spectator frees the member's seats, because only players can be seated.
    """
    campaign = await owned_campaign(db, campaign_id, auth)
    if user_id == campaign.created_by:
        raise HTTPException(status_code=409, detail="The campaign owner's role cannot be changed.")
    membership = await _membership_or_404(db, campaign.id, user_id)
    if membership.role == OWNER_ROLE:
        raise HTTPException(status_code=409, detail="Owner memberships cannot be changed here.")
    if membership.role != body.role:
        campaign_pk = campaign.id
        # Demotion to spectator frees the seats; the spotlight they held is settled in the same commit.
        session_ids = (
            await spotlight.sessions_with_seats(db, campaign_pk, user_id)
            if body.role != PLAYER_ROLE
            else []
        )
        locks = await _lock_tables(redis, session_ids)
        try:
            membership.role = body.role
            changes = []
            if body.role != PLAYER_ROLE:
                changes = await _release_seats(
                    db, redis, campaign_pk, user_id, session_ids, campaign.created_by
                )
            await db.commit()
        finally:
            for lock in locks:
                await lock.release()
        await db.refresh(membership)
        await _announce_changes(redis, changes)
        await _announce_access(db, redis, campaign_pk)
    return await _member_read(db, membership)


async def _remove_membership(
    db, redis: Redis, campaign_id: int, user_id: int, membership, actor_id: int | None
) -> None:
    """Delete a membership and its seats, releasing any spotlight the seats held, in one commit (EDG-048)."""
    session_ids = await spotlight.sessions_with_seats(db, campaign_id, user_id)
    locks = await _lock_tables(redis, session_ids)
    try:
        changes = await _release_seats(db, redis, campaign_id, user_id, session_ids, actor_id)
        await db.delete(membership)
        await db.commit()
    finally:
        for lock in locks:
            await lock.release()
    await _announce_changes(redis, changes)


@router.delete("/campaigns/{campaign_id}/members/{user_id}", status_code=204)
async def remove_member(
    campaign_id: int,
    user_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Owner removes a player or spectator. Their seats are freed; their characters are kept."""
    campaign = await owned_campaign(db, campaign_id, auth)
    if user_id == campaign.created_by:
        raise HTTPException(status_code=409, detail="The campaign owner cannot be removed.")
    membership = await _membership_or_404(db, campaign.id, user_id)
    if membership.role == OWNER_ROLE:
        raise HTTPException(status_code=409, detail="Owner memberships cannot be removed here.")
    campaign_pk = campaign.id
    await _remove_membership(db, redis, campaign_pk, user_id, membership, campaign.created_by)
    await _announce_access(db, redis, campaign_pk)


@router.post("/campaigns/{campaign_id}/leave", status_code=204)
async def leave_campaign(
    campaign_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """A member leaves on their own. Frees their seats; their characters are kept."""
    campaign = await member_campaign(db, campaign_id, auth)
    if auth is None or campaign.created_by == auth.user_id:
        raise HTTPException(
            status_code=409,
            detail="The campaign owner cannot leave. Delete or end the campaign instead.",
        )
    membership = await _membership_or_404(db, campaign.id, auth.user_id)
    if membership.role == OWNER_ROLE:
        raise HTTPException(status_code=409, detail="Owner memberships cannot leave here.")
    campaign_pk = campaign.id
    await _remove_membership(db, redis, campaign_pk, auth.user_id, membership, None)
    await _announce_access(db, redis, campaign_pk)
