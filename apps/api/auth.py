"""Identity and authorization for the API (EDG-065).

Two modes, selected by ``EDGAR_AUTH_MODE``:

* ``local`` (default): the single-user app. ``require_authentication`` returns ``None``, no
  ownership or admin restriction applies, and ``current_user_id`` returns the first user exactly
  as before this module existed.
* ``token`` (hosting): every ``/api`` route requires ``Authorization: Bearer <token>``. Tokens are
  random, stored only as SHA-256 digests in ``api_tokens``, and can be revoked. Campaign, session,
  character and NPC access is limited to the owning user (404 otherwise); adventure-catalogue
  writes, bootstrap, diagnostics and metrics require an admin user (``EDGAR_ADMIN_USER_IDS``).

Startup validation (``validate_auth_configuration``) refuses token mode unless its required
settings are present, so hosting cannot be enabled half-way. See docs/authentication.md.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres import get_session
from db.postgres.models import (
    NPC,
    ApiToken,
    Campaign,
    CampaignMembership,
    Session as SessionModel,
    SessionParticipant,
    User,
)

AUTH_MODE_ENV = "EDGAR_AUTH_MODE"
ADMIN_IDS_ENV = "EDGAR_ADMIN_USER_IDS"
DEBUG_OVERRIDE_ENV = "EDGAR_ALLOW_DEBUG_IN_TOKEN_MODE"

LOCAL = "local"
TOKEN = "token"
TOKEN_PREFIX = "edgar_"
# A token's last_used_at is refreshed at most this often, so a busy client does not write per request.
LAST_USED_WRITE_INTERVAL = timedelta(minutes=5)
_TRUTHY = {"1", "true", "yes"}


class AuthConfigError(Exception):
    """Startup configuration is unsafe for the requested auth mode. Messages name settings, never values."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of a token-mode request."""

    user_id: int
    token_id: int | None
    is_admin: bool


def auth_mode(environ: Mapping[str, str] | None = None) -> str:
    """Return ``local`` only for an unset/empty/``local`` value; anything else fails closed to ``token``."""
    env = os.environ if environ is None else environ
    raw = (env.get(AUTH_MODE_ENV) or "").strip().lower()
    return LOCAL if raw in {"", LOCAL} else TOKEN


def admin_user_ids(environ: Mapping[str, str] | None = None) -> frozenset[int]:
    """Parse ``EDGAR_ADMIN_USER_IDS``; malformed entries are ignored here and rejected at startup."""
    env = os.environ if environ is None else environ
    ids: set[int] = set()
    for part in (env.get(ADMIN_IDS_ENV) or "").split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            ids.add(int(part))
    return frozenset(ids)


def hash_token(token: str) -> str:
    """SHA-256 hex digest. Tokens are 256-bit random values, so a fast digest is sufficient."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """A new high-entropy bearer token. Shown once by the operator tool; never stored in plaintext."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


INVITE_TOKEN_PREFIX = "edginv_"


def generate_invite_token() -> str:
    """A single-use campaign invite token (EDG-046). Shown once at creation; stored only as a digest."""
    return INVITE_TOKEN_PREFIX + secrets.token_urlsafe(32)


def validate_auth_configuration(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Fail fast when the configured auth mode is unsafe to run (EDG-078 startup pattern)."""
    env = os.environ if environ is None else environ
    raw = (env.get(AUTH_MODE_ENV) or "").strip().lower()
    if raw not in {"", LOCAL, TOKEN}:
        raise AuthConfigError(f"{AUTH_MODE_ENV} must be 'local' or 'token'.")
    if auth_mode(env) == LOCAL:
        return {"auth_mode": LOCAL}

    ids_raw = (env.get(ADMIN_IDS_ENV) or "").strip()
    if not ids_raw:
        raise AuthConfigError(
            f"{AUTH_MODE_ENV}=token requires {ADMIN_IDS_ENV} (comma-separated user ids) so that "
            "bootstrap and admin routes have an operator."
        )
    ids = admin_user_ids(env)
    if not ids or any(not part.strip().isdigit() or int(part) <= 0 for part in ids_raw.split(",")):
        raise AuthConfigError(f"{ADMIN_IDS_ENV} must contain only positive integer user ids.")
    if (env.get("EDGAR_DEBUG") or "").strip().lower() in _TRUTHY and (
        (env.get(DEBUG_OVERRIDE_ENV) or "").strip() != "1"
    ):
        raise AuthConfigError(
            "EDGAR_DEBUG is not allowed in token mode. Unset it, or set "
            f"{DEBUG_OVERRIDE_ENV}=1 to override deliberately."
        )
    return {"auth_mode": TOKEN, "admin_count": len(ids)}


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Authentication required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def bearer_token(request: Request) -> str | None:
    """The presented bearer value, or None when the header is absent or not a Bearer credential."""
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    presented = presented.strip()
    if scheme.lower() != "bearer" or not presented:
        return None
    return presented


async def resolve_token(db: AsyncSession, presented: str, *, now: datetime | None = None) -> Principal:
    """Map a presented token to its principal, or raise the generic 401.

    The row is found by digest (the token itself is never queried or stored), then the digest is
    re-compared in constant time. A revoked token is indistinguishable from an unknown one.
    """
    digest = hash_token(presented)
    row = (await db.execute(select(ApiToken).where(ApiToken.token_hash == digest))).scalar_one_or_none()
    if row is None or not hmac.compare_digest(row.token_hash, digest) or row.revoked_at is not None:
        raise _unauthenticated()
    current = now or datetime.now(UTC)
    if row.last_used_at is None or current - row.last_used_at >= LAST_USED_WRITE_INTERVAL:
        row.last_used_at = current
        await db.commit()
    return Principal(
        user_id=row.user_id,
        token_id=row.id,
        is_admin=row.user_id in admin_user_ids(),
    )


async def require_authentication(
    request: Request, db: AsyncSession = Depends(get_session)
) -> Principal | None:
    """Router-level guard. Local mode: no check, returns None. Token mode: verifies the bearer token.

    The token lookup is a read. Ending its transaction with a commit returns the pooled connection
    now (the session factory keeps objects usable after commit), so a long-lived stream (EDG-047)
    does not hold one for its whole life.
    """
    if auth_mode() == LOCAL:
        return None
    presented = bearer_token(request)
    if presented is None:
        raise _unauthenticated()
    principal = await resolve_token(db, presented)
    await db.commit()
    request.state.principal = principal
    return principal


Auth = Annotated[Principal | None, Depends(require_authentication)]


def principal_for(request: Request) -> Principal | None:
    """The principal authenticated for this request, or None (local mode / not yet authenticated).

    Rate limits and spend caps can key on ``principal.user_id`` through this hook.
    """
    return getattr(request.state, "principal", None)


async def get_current_principal(
    auth: Auth, db: AsyncSession = Depends(get_session)
) -> Principal:
    """The principal that owns this request's data.

    Token mode: the authenticated caller. Local mode: the first user (lowest id), or 400 when no
    user exists yet, exactly as the original single-user seam behaved.
    """
    if auth is not None:
        return auth
    result = await db.execute(select(User).order_by(User.id).limit(1))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=400,
            detail="No user found. Open /ui to complete first-run setup.",
        )
    return Principal(user_id=user.id, token_id=None, is_admin=False)


async def require_admin(auth: Auth) -> None:
    """Admin-only routes. Local mode: no restriction. Token mode: the caller must be an admin."""
    if auth is None:
        return
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")


# Ownership and membership (EDG-046). Each helper returns the row, or raises the same 404 the route
# raised for a missing row, so a resource the caller cannot see is indistinguishable from one that
# does not exist. In local mode (principal is None) the lookup is unrestricted, which keeps the
# single-user behaviour unchanged.
#
# Roles, per campaign: the creator is the implicit owner (no membership row); a membership row
# carries ``owner`` or ``player``. ``owned_*`` helpers are owner-only and gate every mutation.
# ``member_*`` helpers admit any member and gate reads. Turns and intros use ``authorize_session``,
# which additionally requires the caller to hold the spotlight (see ``turn_session``).

OWNER_ROLE = "owner"
PLAYER_ROLE = "player"
SPECTATOR_ROLE = "spectator"


def _membership_exists(user_id: int, roles: tuple[str, ...] | None = None) -> Any:
    stmt = select(CampaignMembership.id).where(
        CampaignMembership.campaign_id == Campaign.id,
        CampaignMembership.user_id == user_id,
    )
    if roles is not None:
        stmt = stmt.where(CampaignMembership.role.in_(roles))
    return exists(stmt)


def _owner_filter(user_id: int) -> Any:
    return or_(Campaign.created_by == user_id, _membership_exists(user_id, (OWNER_ROLE,)))


def _member_filter(user_id: int) -> Any:
    return or_(Campaign.created_by == user_id, _membership_exists(user_id))


async def campaign_role(db: AsyncSession, campaign: Campaign, user_id: int) -> str | None:
    """The caller's role in a campaign, or None when they have no access."""
    if campaign.created_by == user_id:
        return OWNER_ROLE
    return await db.scalar(
        select(CampaignMembership.role).where(
            CampaignMembership.campaign_id == campaign.id,
            CampaignMembership.user_id == user_id,
        )
    )


async def authorize_campaign(db: AsyncSession, campaign_id: int, auth: Auth) -> None:
    """Pre-check owner access without loading the row (no-op in local mode)."""
    if auth is not None:
        await owned_campaign(db, campaign_id, auth)


async def owned_campaign(db: AsyncSession, campaign_id: int, auth: Auth) -> Campaign:
    """Owner-only access to a campaign (creator or an owner membership)."""
    stmt = select(Campaign).where(Campaign.id == campaign_id)
    if auth is not None:
        stmt = stmt.where(_owner_filter(auth.user_id))
    campaign = (await db.execute(stmt)).scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


async def member_campaign(db: AsyncSession, campaign_id: int, auth: Auth) -> Campaign:
    """Read access to a campaign for any member (owner or player)."""
    stmt = select(Campaign).where(Campaign.id == campaign_id)
    if auth is not None:
        stmt = stmt.where(_member_filter(auth.user_id))
    campaign = (await db.execute(stmt)).scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


async def owned_session(db: AsyncSession, session_id: int, auth: Auth) -> SessionModel:
    """Owner-only access to a session (used by session mutations)."""
    stmt = select(SessionModel).where(SessionModel.id == session_id)
    if auth is not None:
        stmt = stmt.join(Campaign, Campaign.id == SessionModel.campaign_id).where(
            _owner_filter(auth.user_id)
        )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def member_session(db: AsyncSession, session_id: int, auth: Auth) -> SessionModel:
    """Read access to a session for any campaign member."""
    stmt = select(SessionModel).where(SessionModel.id == session_id)
    if auth is not None:
        stmt = stmt.join(Campaign, Campaign.id == SessionModel.campaign_id).where(
            _member_filter(auth.user_id)
        )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def turn_session(db: AsyncSession, session_id: int, auth: Auth) -> SessionModel:
    """The session a turn or intro may act in (EDG-046).

    The campaign owner may always act (single-owner behaviour is unchanged). A player may act only
    when their own seated character is the session's spotlight holder (``active_character_id``);
    members who are not seated, or seated but not holding the spotlight, get 403. Non-members get
    the same 404 as a missing session.
    """
    session = await member_session(db, session_id, auth)
    if auth is None:
        return session
    campaign = await db.get(Campaign, session.campaign_id)
    if campaign is not None and await campaign_role(db, campaign, auth.user_id) == OWNER_ROLE:
        return session
    seated = await db.scalar(
        select(SessionParticipant.id).where(
            SessionParticipant.session_id == session.id,
            SessionParticipant.user_id == auth.user_id,
            SessionParticipant.character_id == session.active_character_id,
        )
    )
    if seated is None:
        raise HTTPException(
            status_code=403,
            detail="Only the player whose character holds the spotlight can act in this session.",
        )
    return session


async def authorize_session(db: AsyncSession, session_id: int, auth: Auth) -> None:
    """Pre-check before a turn or intro takes the session lock (no-op in local mode)."""
    if auth is not None:
        await turn_session(db, session_id, auth)


async def owned_npc(db: AsyncSession, npc_id: int, auth: Auth) -> NPC:
    stmt = select(NPC).where(NPC.id == npc_id)
    if auth is not None:
        stmt = stmt.join(Campaign, Campaign.id == NPC.campaign_id).where(
            _owner_filter(auth.user_id)
        )
    npc = (await db.execute(stmt)).scalar_one_or_none()
    if npc is None:
        raise HTTPException(status_code=404, detail="NPC not found")
    return npc


def campaign_scope(auth: Auth) -> Any:
    """SQL filter restricting a Campaign query to the caller's campaigns, owned or joined (None locally)."""
    return None if auth is None else _member_filter(auth.user_id)


__all__ = [
    "Auth",
    "AuthConfigError",
    "Principal",
    "authorize_campaign",
    "authorize_session",
    "auth_mode",
    "campaign_role",
    "bearer_token",
    "campaign_scope",
    "generate_invite_token",
    "generate_token",
    "get_current_principal",
    "hash_token",
    "owned_campaign",
    "member_campaign",
    "member_session",
    "owned_npc",
    "owned_session",
    "principal_for",
    "require_admin",
    "require_authentication",
    "resolve_token",
    "turn_session",
    "validate_auth_configuration",
]
