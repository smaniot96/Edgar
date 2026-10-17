from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.postgres.base import Base

if TYPE_CHECKING:
    from db.postgres.models.sessions import Session

RetentionClass = Literal["durable", "diagnostic"]

# Retention class of every event_type the code writes. Durable rows are replay, player history,
# rulings and memory: no expiry may remove them. Diagnostic rows may expire once no consumer needs
# them. tests/db/test_event_retention.py fails for any written type missing here (see db/README.md).
EVENT_RETENTION: dict[str, RetentionClass] = {
    "narration": "durable",
    "adjudication": "durable",
    "memory_summary": "durable",
    "prepared_amendment": "durable",
    "combat_round": "diagnostic",
    "turn_diagnostics": "diagnostic",
    "turn_trace": "diagnostic",
}


class EventLog(Base):
    __tablename__ = "event_log"
    __table_args__ = (
        # Covers the FK lookup and the per-session chronological reads (chat history, recaps).
        Index("ix_event_log_session_id_created_at", "session_id", "created_at"),
        # Summary lookups and the narration range folded into each summary use id order.
        Index("ix_event_log_session_id_event_type_id", "session_id", "event_type", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    session: Mapped[Session] = relationship(back_populates="events")
