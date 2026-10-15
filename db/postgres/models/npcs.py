from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.postgres.base import Base

if TYPE_CHECKING:
    from db.postgres.models.campaigns import Campaign


class NPC(Base):
    __tablename__ = "npcs"
    __table_args__ = (
        UniqueConstraint("session_id", "story_identity", name="uq_npcs_session_story_identity"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Null preserves existing explicitly campaign-wide/manual NPC semantics.
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    story_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    disposition: Mapped[str] = mapped_column(String(255), nullable=False)
    stat_block: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Relationships
    campaign: Mapped[Campaign] = relationship(back_populates="npcs")
