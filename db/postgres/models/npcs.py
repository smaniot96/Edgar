from db.postgres.base import Base
from sqlalchemy import String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB


class NPC(Base):
    __tablename__ = "npcs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    disposition: Mapped[str] = mapped_column(String(255), nullable=False)
    stat_block: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Relationships
    campaign: Mapped["Campaign"] = relationship(back_populates="npcs")  # noqa: F821
