from datetime import datetime

from db.postgres.base import Base
from sqlalchemy import DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship


class CharacterAssignment(Base):
    __tablename__ = "character_assignments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    hp_current: Mapped[int] = mapped_column(Integer, nullable=False)
    hp_max: Mapped[int] = mapped_column(Integer, nullable=False)
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False)
    inventory: Mapped[dict] = mapped_column(JSONB, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    character: Mapped["Character"] = relationship(  # noqa: F821
        back_populates="assignments", passive_deletes=True
    )
    campaign: Mapped["Campaign"] = relationship(back_populates="character_assignments")  # noqa: F821
