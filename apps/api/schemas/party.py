"""Campaign invites and session participant roster (EDG-046).

The plaintext invite token appears only in `InviteCreated`, the response to its creation. Every
other read shape omits it, and no shape ever carries the stored digest.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

InviteRole = Literal["player", "spectator"]
InviteStatus = Literal["active", "accepted", "revoked", "expired"]


class InviteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: InviteRole = "player"
    session_id: int | None = Field(default=None, gt=0)
    invited_email: str | None = Field(default=None, min_length=3, max_length=255)
    expires_in_hours: int = Field(default=72, ge=1, le=720)


class InviteRead(BaseModel):
    id: int
    campaign_id: int
    session_id: int | None
    role: InviteRole
    invited_email: str | None
    status: InviteStatus
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None


class InviteCreated(InviteRead):
    token: str = Field(description="Shown once. Store it now; it cannot be retrieved later.")


class InviteAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=10, max_length=200)
    character_id: int | None = Field(default=None, gt=0)


class InviteAccepted(BaseModel):
    campaign_id: int
    role: InviteRole
    session_id: int | None
    character_id: int | None


class SessionParticipantRead(BaseModel):
    user_id: int
    display_name: str | None
    character_id: int
    character_name: str
    role: Literal["player"]
    joined_at: datetime
