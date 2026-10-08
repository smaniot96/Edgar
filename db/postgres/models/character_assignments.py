from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.postgres.base import Base

if TYPE_CHECKING:
    from db.postgres.models.campaigns import Campaign
    from db.postgres.models.characters import Character


class CharacterAssignment(Base):
    __tablename__ = "character_assignments"
    __table_args__ = (
        # A character can be active in at most one campaign at a time (migration 8).
        Index(
            "character_assignments_one_active",
            "character_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        CheckConstraint("hp_current <= hp_max", name="character_assignments_hp_within_max"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hp_current: Mapped[int] = mapped_column(Integer, nullable=False)
    hp_max: Mapped[int] = mapped_column(Integer, nullable=False)
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False)
    inventory: Mapped[dict] = mapped_column(JSONB, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    character: Mapped[Character] = relationship(back_populates="assignments")
    campaign: Mapped[Campaign] = relationship(back_populates="character_assignments")
