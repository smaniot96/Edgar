from datetime import datetime

from db.postgres.base import Base
from sqlalchemy import ForeignKey, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    current_scene_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active_character_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("characters.id"), nullable=True
    )

    # Relationships
    campaign: Mapped["Campaign"] = relationship(back_populates="sessions")  # noqa: F821
    character: Mapped["Character | None"] = relationship(foreign_keys=[active_character_id])  # noqa: F821
    combat_states: Mapped[list["CombatState"]] = relationship(back_populates="session")  # noqa: F821
    events: Mapped[list["EventLog"]] = relationship(back_populates="session")  # noqa: F821
