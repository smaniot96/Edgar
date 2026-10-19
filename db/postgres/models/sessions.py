from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.postgres.base import Base

if TYPE_CHECKING:
    from db.postgres.models.campaigns import Campaign
    from db.postgres.models.characters import Character
    from db.postgres.models.combat_states import CombatState
    from db.postgres.models.event_logs import EventLog


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("campaign_id", "creation_request_id", name="uq_session_creation_request"),
        CheckConstraint(
            "spotlight_mode IN ('round_robin', 'owner_directed', 'free_for_all')",
            name="sessions_spotlight_mode_valid",
        ),
        CheckConstraint("spotlight_version >= 0", name="sessions_spotlight_version_nonnegative"),
        CheckConstraint(
            "spotlight_timeout_seconds BETWEEN 30 AND 3600",
            name="sessions_spotlight_timeout_valid",
        ),
        CheckConstraint(
            "pacing_policy IN ('relaxed', 'standard', 'fast', 'custom')",
            name="sessions_pacing_policy_valid",
        ),
    )
    creation_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    creation_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    # Spotlight arbitration for multi-PC tables (EDG-048). `active_character_id` is the holder;
    # every holder or rotation change bumps `spotlight_version`, so a stale client is refused.
    spotlight_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'round_robin'")
    )
    spotlight_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    spotlight_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("300")
    )
    spotlight_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The round-robin rotation: character ids in turn order.
    spotlight_order: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # Free-for-all cooldowns: character id (as text) -> ISO time the cooldown ends.
    spotlight_cooldowns: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Table pacing (EDG-051): the named policy the whole table sees, and the one queued intended
    # action each waiting player may hold: {"revision": n, "items": {"<user_id>": {...}}}.
    pacing_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'standard'")
    )
    queued_actions: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
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
