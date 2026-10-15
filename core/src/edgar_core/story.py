"""Small shared contract for proposed, validated and committed session story facts.

Definitions remain in reviewed manifests. A fact is earned in one playthrough and carries
immutable source identity; neither display names nor model prose establishes knowledge.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from edgar_core.manifests import Digest, Identifier

StoryKind = Literal[
    "discover",
    "encounter",
    "visit",
    "move",
    "objective",
    "milestone",
    "promise",
    "clock",
    "relationship",
    "outcome",
    "acquire",
    "consume",
    "transfer",
    "use",
    "environment",
]
StoryStatus = Literal[
    "discovered",
    "encountered",
    "visited",
    "active",
    "completed",
    "failed",
    "fulfilled",
    "broken",
    "advanced",
    "friendly",
    "neutral",
    "hostile",
    "acquired",
    "consumed",
    "moved",
    "used",
    "changed",
]


class StoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StoryRef(StoryRecord):
    collection: str = Field(min_length=1, max_length=200)
    source_sha256: Digest
    manifest_sha256: Digest
    kind: Literal["scene", "entity", "object", "objective", "milestone", "promise", "clock"]
    canonical_id: Identifier


class StoryEffect(StoryRecord):
    kind: StoryKind
    target: StoryRef
    status: StoryStatus = "discovered"
    source_reference_ids: list[str] = Field(default_factory=list, max_length=12)
    knowledge_fact_ids: list[str] = Field(default_factory=list, max_length=12)
    # A proposal only. Validation replaces this with exact eligible player source text.
    player_text: str | None = Field(default=None, max_length=12000)
    quantity: int | None = Field(default=None, ge=0, le=1000)
    destination: str | None = Field(default=None, max_length=200)
    operation: Identifier | None = None


class AcceptedStoryEffect(StoryEffect):
    effect_id: Digest
    turn_id: str = Field(min_length=1, max_length=100)
    display_name: str = Field(default="", max_length=200)
    # Full existing citation records, copied by the validator, never model supplied.
    source_refs: list[dict] = Field(default_factory=list)


class StoryFact(AcceptedStoryEffect):
    fact_id: Digest
    event_id: int | None = None


class StoryState(StoryRecord):
    facts: list[StoryFact] = Field(default_factory=list)


def story_target_id(target: StoryRef) -> str:
    """Stable fact identity across status updates, qualified by immutable definition."""
    return hashlib.sha256(
        json.dumps(target.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def accept_story_effect(
    effect: StoryEffect, *, turn_id: str, source_refs: list[dict], display_name: str = ""
) -> AcceptedStoryEffect:
    """Seal a previously validated effect; identity deduplicates static facts and per-turn mutable operations."""
    body = effect.model_dump()
    body["source_reference_ids"] = sorted(set(body["source_reference_ids"]))
    body["knowledge_fact_ids"] = sorted(set(body["knowledge_fact_ids"]))
    identity = dict(body)
    if effect.kind in {
        "acquire",
        "consume",
        "transfer",
        "use",
        "environment",
        "relationship",
        "move",
    }:
        identity["turn_id"] = turn_id
    effect_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    source_refs = sorted(
        {ref["reference_id"]: ref for ref in source_refs}.values(),
        key=lambda ref: ref["reference_id"],
    )
    return AcceptedStoryEffect(
        **body,
        effect_id=effect_id,
        turn_id=turn_id,
        source_refs=source_refs,
        display_name=display_name,
    )
