from datetime import datetime

from db.postgres.base import Base
from sqlalchemy import Boolean, ForeignKey, Integer, DateTime, String, func, text
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
    # Full combatant roster with per-entity HP/stats (player + enemies). One round of combat
    # is resolved per HTTP turn; enemy deaths and end conditions are derived from this list.
    combatants: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # 'victory' | 'defeat' | 'fled' | None (still in progress).
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    session: Mapped["Session"] = relationship(back_populates="combat_states")  # noqa: F821
    active_char: Mapped["Character | None"] = relationship()  # noqa: F821
