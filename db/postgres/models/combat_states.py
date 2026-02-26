from db.postgres.base import Base
from sqlalchemy import String, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

class CombatState(Base):
    __tablename__ = "combat_state"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    initiative_order: Mapped[list] = mapped_column(JSONB, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    active_character: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
