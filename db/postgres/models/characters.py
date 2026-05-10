from datetime import datetime

from db.postgres.base import Base
from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship


class Character(Base):
    __tablename__ = "characters"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    character_class: Mapped[str] = mapped_column("class", String(255), nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    hp_max: Mapped[int] = mapped_column(Integer, nullable=False)
    base_stats: Mapped[dict] = mapped_column(JSONB, nullable=False)
    base_inventory: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    owner: Mapped["User"] = relationship(back_populates="characters")  # noqa: F821
    assignments: Mapped[list["CharacterAssignment"]] = relationship(  # noqa: F821
        back_populates="character",
        passive_deletes=True,
    )
