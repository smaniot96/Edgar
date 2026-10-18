from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.postgres.base import Base


class CampaignInvite(Base):
    """A single-use, expiring invitation to join a campaign (EDG-046).

    Only the SHA-256 digest of the invite token is stored (``token_hash``); the plaintext is
    returned once, when the invite is created. Acceptance consumes the row by setting
    ``accepted_at``; revocation sets ``revoked_at``. Rows are never reused or hard-deleted by the
    API, so the history of who was invited and when remains.

    ``session_id`` (optional) binds the invite to one session. A ``player`` invite then also seats
    the accepting user's own character there; a ``spectator`` invite only grants watch access. ``invited_email`` (optional) restricts acceptance to one account.
    """

    __tablename__ = "campaign_invites"
    __table_args__ = (
        CheckConstraint("role IN ('player', 'spectator')", name="campaign_invites_role_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="player")
    invited_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
