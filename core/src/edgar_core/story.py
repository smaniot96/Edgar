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
    "learn_place",
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
    "known",
    "announced",
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
    # Engine-owned combat outcomes of kind `encounter` (EDG-146). Never model-proposed.
    "captive",
    "released",
    "dead",
    "defeated",
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


class ClockDisclosure(StoryRecord):
    current: bool = False
    maximum: int | None = Field(default=None, ge=1, le=1000)
    completion: bool = False


class AcceptedStoryEffect(StoryEffect):
    effect_id: Digest
    turn_id: str = Field(min_length=1, max_length=100)
    display_name: str = Field(default="", max_length=200)
    # Full existing citation records, copied by the validator, never model supplied.
    source_refs: list[dict] = Field(default_factory=list)
    evidence_kind: Literal["source", "player_utterance"] = "source"
    origin_turn_id: str | None = None
    # Validator-owned metadata, never accepted from a model's StoryEffect proposal.
    knowledge_kind: Literal["hint"] | None = None
    clock_disclosure: ClockDisclosure | None = None
    related_object: StoryRef | None = None


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
    knowledge_kind: Literal["hint"] | None = None,
    clock_disclosure: ClockDisclosure | None = None,
    related_object: StoryRef | None = None,
) -> AcceptedStoryEffect:
    """Seal a previously validated effect; identity deduplicates static facts and per-turn mutable operations."""
    body = effect.model_dump()
    body["source_reference_ids"] = sorted(set(body["source_reference_ids"]))
    body["knowledge_fact_ids"] = sorted(set(body["knowledge_fact_ids"]))
    identity = dict(body)
    for name, value in (
        ("knowledge_kind", knowledge_kind),
        ("clock_disclosure", clock_disclosure),
        ("related_object", related_object),
    ):
        if value is not None:
            identity[name] = value.model_dump() if isinstance(value, BaseModel) else value
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
        knowledge_kind=knowledge_kind,
        clock_disclosure=clock_disclosure,
        related_object=related_object,
    )


def player_promise_ref(actor: StoryRef, quotation: str) -> StoryRef:
    """An event-authored promise identity, rooted in an already encountered actor."""
    identity = hashlib.sha256((actor.model_dump_json() + "\n" + quotation).encode()).hexdigest()
    return actor.model_copy(update={"kind": "promise", "canonical_id": "promise_" + identity[:48]})


# EDG-169: homebrew play has no reviewed manifest. Its improvised places, people, clues and
# threats are sealed under one engine-owned pseudo-collection whose digests are constants, so
# identity depends only on kind and normalized name, and provenance is never model-supplied.
HOMEBREW_COLLECTION = "homebrew"
HOMEBREW_KINDS = {
    "place": ("learn_place", "scene", "known"),
    "person": ("encounter", "entity", "encountered"),
    "clue": ("discover", "entity", "discovered"),
    "threat": ("discover", "entity", "discovered"),
}
_HOMEBREW_SOURCE = hashlib.sha256(b"edgar:homebrew-source:v1").hexdigest()
_HOMEBREW_MANIFEST = hashlib.sha256(b"edgar:homebrew-manifest:v1").hexdigest()


def homebrew_name_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def homebrew_target(proposal_kind: str, name: str) -> StoryRef:
    """Stable canonical identity of a homebrew improvisation, independent of its prose."""
    digest = hashlib.sha256(homebrew_name_key(name).encode()).hexdigest()[:40]
    return StoryRef(
        collection=HOMEBREW_COLLECTION,
        source_sha256=_HOMEBREW_SOURCE,
        manifest_sha256=_HOMEBREW_MANIFEST,
        kind=HOMEBREW_KINDS[proposal_kind][1],
        canonical_id=f"{proposal_kind}_{digest}",
    )


def homebrew_reference(turn_id: str, target: StoryRef) -> dict:
    """Engine-owned provenance for one homebrew fact; it is never a player-page citation."""
    return {
        "reference_id": hashlib.sha256(
            f"edgar:homebrew:{turn_id}:{target.canonical_id}".encode()
        ).hexdigest(),
        "audience": "homebrew",
        "provenance": "homebrew",
        "collection": HOMEBREW_COLLECTION,
        "source_sha256": _HOMEBREW_SOURCE,
        "extraction_sha256": _HOMEBREW_SOURCE,
        "manifest_sha256": _HOMEBREW_MANIFEST,
    }


def is_homebrew_effect(effect: StoryEffect) -> bool:
    return effect.target.collection == HOMEBREW_COLLECTION


def homebrew_name_taken(name: str, facts: list[StoryFact]) -> bool:
    """A homebrew name may not reuse or overwrite any earned display name in this ledger."""
    key = homebrew_name_key(name)
    return any(fact.display_name and homebrew_name_key(fact.display_name) == key for fact in facts)


def known_story_name(fact: AcceptedStoryEffect) -> str:
    """A prepared identity is not a learned name until it occurs in earned public text."""
    return (
        fact.display_name
        if fact.display_name and fact.display_name.casefold() in (fact.player_text or "").casefold()
        else ""
    )


def _public_origin(ref: dict, effect: AcceptedStoryEffect) -> bool:
    if is_homebrew_effect(effect):
        return (
            ref.get("audience") == "homebrew"
            and ref.get("provenance") == "homebrew"
            and ref.get("collection") == HOMEBREW_COLLECTION
            and ref.get("source_sha256") == effect.target.source_sha256
            and ref.get("manifest_sha256") == effect.target.manifest_sha256
            and all(
                isinstance(ref.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", ref[key])
                for key in ("reference_id", "source_sha256", "extraction_sha256")
            )
        )
    return (
        ref.get("audience") == "player"
        and all(
            isinstance(ref.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", ref[key])
            for key in ("reference_id", "source_sha256", "extraction_sha256")
        )
        and ref.get("collection") == effect.target.collection
        and ref.get("source_sha256") == effect.target.source_sha256
        and ref.get("manifest_sha256") == effect.target.manifest_sha256
        and type(ref.get("page")) is int
        and ref["page"] > 0
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
                "provenance",
            )
            if key in ref
        }
        for ref in effect.source_refs
        if _public_origin(ref, effect)
    ]
    if not origins:
        return None
    return {
        "provenance": "homebrew" if is_homebrew_effect(effect) else "module",
        "effect_id": effect.effect_id,
        "kind": effect.kind,
        "status": (
            "advanced"
            if effect.kind == "clock"
            and not (effect.clock_disclosure and effect.clock_disclosure.completion)
            else effect.status
        ),
        "name": known_story_name(effect),
        "text": effect.player_text,
        "quantity": (
            effect.quantity
            if effect.kind != "clock"
            or (effect.clock_disclosure and effect.clock_disclosure.current)
            else None
        ),
        "clock_maximum": effect.clock_disclosure.maximum if effect.clock_disclosure else None,
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
