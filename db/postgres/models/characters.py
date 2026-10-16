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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.postgres.base import Base

if TYPE_CHECKING:
    from db.postgres.models.character_assignments import CharacterAssignment
    from db.postgres.models.users import User


class Character(Base):
    __tablename__ = "characters"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "creation_request_id", name="uq_character_creation_request"),
        CheckConstraint("level BETWEEN 1 AND 20", name="characters_level_valid"),
        CheckConstraint("hp_max >= 1", name="characters_hp_max_positive"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    creation_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    creation_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    character_class: Mapped[str] = mapped_column("class", String(255), nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    hp_max: Mapped[int] = mapped_column(Integer, nullable=False)
    base_stats: Mapped[dict] = mapped_column(JSONB, nullable=False)
    base_inventory: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    owner: Mapped[User] = relationship(back_populates="characters")
    assignments: Mapped[list[CharacterAssignment]] = relationship(
        back_populates="character",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
