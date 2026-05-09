from datetime import datetime

from db.postgres.base import Base
from sqlalchemy import String, ForeignKey, DateTime, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    system: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    adventure_collections: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        server_default=text("'{}'::text[]"),
    )

    # Relationships
    creator: Mapped["User"] = relationship(back_populates="campaigns")  # noqa: F821
    sessions: Mapped[list["Session"]] = relationship(back_populates="campaign")  # noqa: F821
    characters: Mapped[list["Character"]] = relationship(back_populates="campaign")  # noqa: F821
    npcs: Mapped[list["NPC"]] = relationship(back_populates="campaign")  # noqa: F821
    world_flags: Mapped[list["WorldFlag"]] = relationship(back_populates="campaign")  # noqa: F821
