from db.postgres.base import Base

from sqlalchemy import String, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB


class Character(Base):
    __tablename__ = "characters"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    character_class: Mapped[str] = mapped_column("class", String(255), nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    hp_current: Mapped[int] = mapped_column(Integer, nullable=False)
    hp_max: Mapped[int] = mapped_column(Integer, nullable=False)
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False)
    inventory: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Relationships
    campaign: Mapped["Campaign"] = relationship(back_populates="characters")  # noqa: F821
