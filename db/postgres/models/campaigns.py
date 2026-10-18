from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.postgres.base import Base

if TYPE_CHECKING:
    from db.postgres.models.character_assignments import CharacterAssignment
    from db.postgres.models.npcs import NPC
    from db.postgres.models.sessions import Session
    from db.postgres.models.users import User
    from db.postgres.models.world_flags import WorldFlag


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'ended')", name="campaigns_status_valid"),
        CheckConstraint(
            "source_mode IN ('module', 'homebrew')", name="campaigns_source_mode_valid"
        ),
        CheckConstraint("rules_edition IN ('2014', '2024')", name="campaigns_rules_edition_valid"),
        CheckConstraint(
            "progression_mode IN ('fixed', 'milestone')", name="campaigns_progression_mode_valid"
        ),
        CheckConstraint(
            "death_mode IN ('subdued', 'death_saves')", name="campaigns_death_mode_valid"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    system: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    adventure_collections: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        server_default=text("'{}'::text[]"),
    )
    source_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'module'")
    )
    rules_edition: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'2014'")
    )
    # EDG-045: explicit modes. The server defaults are the behaviour existing campaigns already had.
    progression_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'fixed'")
    )
    death_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'subdued'")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships. Child rows are removed by the database (ON DELETE CASCADE); passive_deletes
    # stops the ORM from loading them just to null out a NOT NULL foreign key.
    creator: Mapped[User] = relationship(back_populates="campaigns")
    sessions: Mapped[list[Session]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )
    character_assignments: Mapped[list[CharacterAssignment]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )
    npcs: Mapped[list[NPC]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )
    world_flags: Mapped[list[WorldFlag]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )
