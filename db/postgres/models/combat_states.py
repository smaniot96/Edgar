from datetime import datetime

from db.postgres.base import Base
from sqlalchemy import Boolean, ForeignKey, Integer, DateTime, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB


class CombatState(Base):
    __tablename__ = "combat_state"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    initiative_order: Mapped[list] = mapped_column(JSONB, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    current_turn_index: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    active_character: Mapped[int | None] = mapped_column(
        ForeignKey("characters.id"), nullable=True
    )
    ended: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    session: Mapped["Session"] = relationship(back_populates="combat_states")  # noqa: F821
    active_char: Mapped["Character | None"] = relationship()  # noqa: F821
