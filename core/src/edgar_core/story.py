"""Small shared contract for proposed, validated and committed session story facts.

Definitions remain in reviewed manifests. A fact is earned in one playthrough and carries
immutable source identity; neither display names nor model prose establishes knowledge.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
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
    promise_actor: StoryRef | None = None


class AcceptedStoryEffect(StoryEffect):
    effect_id: Digest
    turn_id: str = Field(min_length=1, max_length=100)
    display_name: str = Field(default="", max_length=200)
    # Full existing citation records, copied by the validator, never model supplied.
    source_refs: list[dict] = Field(default_factory=list)
    evidence_kind: Literal["source", "player_utterance"] = "source"
    origin_turn_id: str | None = None


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
    effect: StoryEffect,
    *,
    turn_id: str,
    source_refs: list[dict],
    display_name: str = "",
    evidence_kind: Literal["source", "player_utterance"] = "source",
    origin_turn_id: str | None = None,
) -> AcceptedStoryEffect:
    """Seal a previously validated effect; identity deduplicates static facts and per-turn mutable operations."""
    body = effect.model_dump()
    body["source_reference_ids"] = sorted(set(body["source_reference_ids"]))
    body["knowledge_fact_ids"] = sorted(set(body["knowledge_fact_ids"]))
    identity = dict(body)
    if identity["promise_actor"] is None:
        identity.pop("promise_actor")
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
        evidence_kind=evidence_kind,
        origin_turn_id=origin_turn_id,
    )


def player_promise_ref(actor: StoryRef, quotation: str) -> StoryRef:
    """An event-authored promise identity, rooted in an already encountered actor."""
    identity = hashlib.sha256((actor.model_dump_json() + "\n" + quotation).encode()).hexdigest()
    return actor.model_copy(update={"kind": "promise", "canonical_id": "promise_" + identity[:48]})


def known_story_name(fact: AcceptedStoryEffect) -> str:
    """A prepared identity is not a learned name until it occurs in earned public text."""
    return (
        fact.display_name
        if fact.display_name and fact.display_name.casefold() in (fact.player_text or "").casefold()
        else ""
    )


def player_story_effect(effect: AcceptedStoryEffect) -> dict | None:
    """Shared player allowlist for an accepted consequence or committed fact."""
    origins = [
        {
            key: copy.deepcopy(ref[key])
            for key in (
                "reference_id",
                "collection",
                "source_sha256",
                "extraction_sha256",
                "manifest_sha256",
                "page",
                "page_end",
                "source_pages",
            )
            if key in ref
        }
        for ref in effect.source_refs
        if ref.get("audience") == "player"
        and all(
            isinstance(ref.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", ref[key])
            for key in ("reference_id", "source_sha256", "extraction_sha256")
        )
        and ref.get("collection") == effect.target.collection
        and ref.get("source_sha256") == effect.target.source_sha256
        and ref.get("manifest_sha256") == effect.target.manifest_sha256
        and type(ref.get("page")) is int
        and ref["page"] > 0
    ]
    if not origins:
        return None
    return {
        "effect_id": effect.effect_id,
        "kind": effect.kind,
        "status": effect.status,
        "name": known_story_name(effect),
        "text": effect.player_text,
        "quantity": effect.quantity,
        "origins": origins,
        "evidence_kind": effect.evidence_kind,
        "origin_turn_id": effect.origin_turn_id,
    }


def player_known_story(state: dict) -> dict:
    """EDG-041 contract: committed earned facts, without prepared definitions or object bookkeeping."""
    facts = []
    for fact in StoryState.model_validate(state).facts:
        if fact.kind in {"acquire", "consume", "transfer", "use", "environment"}:
            continue
        public = player_story_effect(fact)
        if public is not None:
            facts.append({**public, "fact_id": fact.fact_id, "event_id": fact.event_id})
    return {"facts": facts}
