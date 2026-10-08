from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.postgres.base import Base

if TYPE_CHECKING:
    from db.postgres.models.campaigns import Campaign


class WorldFlag(Base):
    __tablename__ = "world_flags"
    __table_args__ = (
        # One value per key per campaign; the leading campaign_id also serves the FK lookups.
        UniqueConstraint("campaign_id", "key", name="uq_world_flags_campaign_id_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)

    # Relationships
    campaign: Mapped[Campaign] = relationship(back_populates="world_flags")
