from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.postgres.base import Base


class CampaignMembership(Base):
    """A user's role in a campaign other than the creator (EDG-046).

    Roles: ``owner`` (co-DM; accepted by the schema but never minted by the M1 API), ``player``
    (may take turns with a seated character) and ``spectator`` (reads the campaign and its sessions,
    never acts or seats a character). The creator is the implicit owner and has no row here.
    """

    __tablename__ = "campaign_memberships"
    __table_args__ = (
        UniqueConstraint("campaign_id", "user_id", name="uq_campaign_membership_user"),
        CheckConstraint(
            "role IN ('owner', 'player', 'spectator')", name="campaign_memberships_role_valid"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
