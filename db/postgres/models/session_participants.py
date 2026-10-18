from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.postgres.base import Base


class SessionParticipant(Base):
    """A user's character seated at a session's table (EDG-046).

    One seat per user and one per character in a session. The seated character is always owned by
    the seated user. The session's ``active_character_id`` is the spotlight holder; the roster is
    the party the agent addresses.
    """

    __tablename__ = "session_participants"
    __table_args__ = (
        UniqueConstraint("session_id", "user_id", name="uq_session_participant_user"),
        UniqueConstraint("session_id", "character_id", name="uq_session_participant_character"),
        CheckConstraint("role IN ('player')", name="session_participants_role_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="player")
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
