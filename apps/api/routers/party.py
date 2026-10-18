"""Campaign membership invites and the session participant roster (EDG-046).

Invites are created and listed by the campaign owner only. Accepting one requires an authenticated
caller (token mode); the accepting user must own the character they seat. Token-level failures
(unknown, expired, revoked, already used, wrong invited account) share one generic 404 so the
response never reveals which condition applied.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
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
    PLAYER_ROLE,
    Auth,
    generate_invite_token,
    hash_token,
    member_session,
    owned_campaign,
)
from ..dependencies import current_user_id, get_session
from ..schemas.party import (
    InviteAccept,
    InviteAccepted,
    InviteCreate,
    InviteCreated,
    InviteRead,
    SessionParticipantRead,
)
from ..services.character_assignments import load_active_assignment

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

    # Consume the invite in one conditional UPDATE, so two concurrent accepts cannot both win.
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
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="The invite could not be applied.") from exc
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
