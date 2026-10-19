"""Spotlight arbitration shapes for multi-PC tables (EDG-048).

The read shape is the table's public spotlight: who holds it, the rule that governs it, the version
a client must quote, and the next-up order. It carries display names and character names only.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .pacing import PacingPolicyName, PacingRead, QueueRead

SpotlightMode = Literal["round_robin", "owner_directed", "free_for_all"]


class SpotlightMember(BaseModel):
    character_id: int
    character_name: str
    user_id: int
    display_name: str | None = None
    present: bool = False
    # EDG-051: this player has a queued action waiting (next_up only).
    queued: bool = False


class SpotlightHolder(SpotlightMember):
    eligible: bool = True


class CombatTurnRead(BaseModel):
    """A running shared fight (EDG-049): who acts next and whether that turn may be skipped."""

    round: int | None = None
    holder_character_id: int | None = None
    skip_due: bool = False


class SpotlightRead(BaseModel):
    session_id: int
    # False for solo and single-PC tables, where the rules are not engaged.
    engaged: bool
    mode: SpotlightMode
    version: int
    timeout_seconds: int
    holder: SpotlightHolder | None = None
    since: datetime | None = None
    # The holder has been idle past the timeout; the next table request applies the handover.
    handover_due: bool = False
    # EDG-051: when an idle holder loses the spotlight (null when it never times out).
    turn_deadline: datetime | None = None
    next_up: list[SpotlightMember] = Field(default_factory=list)
    pacing: PacingRead
    queue: QueueRead
    # Additive (EDG-049): present while a fight with two or more heroes is running.
    combat: CombatTurnRead | None = None


class SpotlightUpdate(BaseModel):
    """Owner-only changes. Omit a field to leave it alone; send null for holder_character_id to release."""

    model_config = ConfigDict(extra="forbid")

    mode: SpotlightMode | None = None
    timeout_seconds: int | None = Field(default=None, ge=30, le=3600)
    holder_character_id: int | None = Field(default=None, ge=1)
    expected_version: int | None = Field(default=None, ge=0)
    # EDG-051: a named table pacing policy (sets the timeout). Not combinable with timeout_seconds.
    pacing: PacingPolicyName | None = None

    @model_validator(mode="after")
    def _one_timing_source(self) -> "SpotlightUpdate":
        if self.pacing is not None and self.timeout_seconds is not None:
            raise ValueError("Choose a pacing policy or an explicit timeout, not both.")
        return self
