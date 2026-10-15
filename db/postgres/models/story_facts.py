"""Append-only, session-owned accepted story effects with event provenance.

Prepared adventure definitions stay in reviewed manifests. This table contains only facts
actually accepted in a playthrough; replaying its payloads is the canonical story state.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.postgres.base import Base


class StoryFact(Base):
    __tablename__ = "story_facts"
    __table_args__ = (
        UniqueConstraint("session_id", "effect_id", name="uq_story_facts_session_effect"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    effect_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("event_log.id", ondelete="CASCADE"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
