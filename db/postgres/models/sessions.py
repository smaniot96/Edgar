from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.postgres.base import Base

if TYPE_CHECKING:
    from db.postgres.models.campaigns import Campaign
    from db.postgres.models.characters import Character
    from db.postgres.models.combat_states import CombatState
    from db.postgres.models.event_logs import EventLog


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_scene_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Deleting the character keeps the session (and its history) but clears the pointer.
    active_character_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Relationships. Children are deleted by the database (ON DELETE CASCADE).
    campaign: Mapped[Campaign] = relationship(back_populates="sessions")
    character: Mapped[Character | None] = relationship(foreign_keys=[active_character_id])
    combat_states: Mapped[list[CombatState]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )
    events: Mapped[list[EventLog]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )
