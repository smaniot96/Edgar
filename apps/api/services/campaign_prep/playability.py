"""Preparation playability evidence and exact capability review receipts.

Event links are executable navigation, so deleting a bad reference cannot make an
orphaned encounter acceptable. Semantic review still decides which reachable
sinks are legitimate endings and which spatial exits are needed.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from edgar_core.campaign_semantics import Record

from .store import NeedsAttention, digest

COMPATIBILITY_POLICY = "required-capabilities-1"


class CapabilityDecision(Record):
    capability_id: str
    disposition: Literal["optional", "supported_adaptation", "blocked"]
    required_for_completion: bool
    record_ids: list[str] = Field(min_length=1)
    evidence: str = Field(min_length=1)
    alternative_quote: str = Field(
        description="Exact existing authored quote establishing a supported alternative; empty otherwise."
    )


class CompatibilityReview(Record):
    decisions: list[CapabilityDecision]


def topology(spine, bundles):
    """Mirror compiler links, including its explicit chapter boundary fallback."""
    by_chapter = {bundle["chapter_id"]: bundle for bundle in bundles["chapters"]}
    chapters = sorted(spine["chapters"], key=lambda chapter: (chapter["play_order"], chapter["id"]))
    events = [event for chapter in chapters for event in by_chapter[chapter["id"]]["events"]]
    links = {event["id"]: list(event["next_event_ids"]) for event in events}
    for index, chapter in enumerate(chapters[:-1]):
        current = by_chapter[chapter["id"]]["events"]
        following = by_chapter[chapters[index + 1]["id"]]["events"]
        if current and following:
            links[current[-1]["id"]].append(following[0]["id"])
    incoming = {event["id"]: [] for event in events}
    for start, targets in links.items():
        links[start] = sorted(set(targets) - {start})
        for target in links[start]:
            if target in incoming:
                incoming[target].append(start)
    entry = events[0]["id"] if events else None
    reached, pending = set(), [entry] if entry else []
    while pending:
        key = pending.pop()
        if key in reached or key not in links:
            continue
        reached.add(key)
        pending.extend(links[key])
    return {
        "entry_event_id": entry,
        "links": links,
        "incoming": incoming,
        "unreachable_event_ids": sorted(set(links) - reached),
        "sink_event_ids": sorted(key for key, values in links.items() if not values),
        "event_viewpoints": [
            {
                "event_id": event["id"],
                "location_id": event["location_id"],
                "trigger": event["trigger"],
                "public_description": event["public_description"],
                "dm_notes": event["dm_notes"],
            }
            for event in events
        ],
    }


def topology_findings(spine, bundles):
    graph = topology(spine, bundles)
    if not graph["unreachable_event_ids"]:
        return []
    # All event records are supplied because the missing edge's *source* can be
    # anywhere in the campaign. This is a graph defect, not an orphan-only edit.
    return [
        {
            "id": "topology_" + digest(graph["unreachable_event_ids"])[:16],
            "severity": "material",
            "record_ids": sorted(graph["links"]),
            "evidence": "Prepared events have no directed route from the campaign entry: "
            + ", ".join(graph["unreachable_event_ids"])
            + ". Current executable links: "
            + json.dumps(graph["links"], sort_keys=True),
            "instruction": "Restore source-grounded conditional progression links to existing "
            "events, including escalation/finale and credible withdrawal or return routes. "
            "Use stable existing IDs; do not remove events, invent completion, or connect "
            "unrelated scenes just to satisfy reachability. Event activation never implies "
            "physical entry or disclosure of unseen rooms.",
        }
    ]


def capability_inventory(compiled):
    return [
        {"capability_id": digest(finding), **finding}
        for finding in compiled.get("capability_findings", [])
    ]


def compatibility_signature(compiled):
    return digest(
        {
            "policy": COMPATIBILITY_POLICY,
            "semantic_sha256": compiled["semantic_sha256"],
            "manifest_sha256": compiled["manifest_sha256"],
            "capabilities": capability_inventory(compiled),
        }
    )


def _contains_quote(value, quote):
    if isinstance(value, str):
        return quote in value
    if isinstance(value, dict):
        return any(_contains_quote(row, quote) for row in value.values())
    if isinstance(value, list):
        return any(_contains_quote(row, quote) for row in value)
    return False


def compatibility_findings(compiled, review):
    inventory = capability_inventory(compiled)
    expected = {row["capability_id"] for row in inventory}
    decisions = review.decisions
    if len(decisions) != len(expected) or {row.capability_id for row in decisions} != expected:
        raise NeedsAttention(
            "Capability review must assess every exact compiled capability gap once."
        )
    registry = compiled["registry"]
    findings = []
    for decision in decisions:
        if not set(decision.record_ids) <= registry.keys():
            raise NeedsAttention("Capability review references nonexistent authored records.")
        if decision.disposition == "optional" and decision.required_for_completion:
            raise NeedsAttention("A required capability cannot be classified as optional.")
        if decision.disposition == "supported_adaptation":
            if not decision.alternative_quote.strip() or not any(
                _contains_quote(registry[key], decision.alternative_quote)
                for key in decision.record_ids
            ):
                raise NeedsAttention(
                    "Supported adaptation requires exact existing authored evidence."
                )
        if decision.disposition == "blocked":
            findings.append(
                {
                    "id": "capability_" + decision.capability_id[:16],
                    "severity": "material",
                    "record_ids": decision.record_ids,
                    "evidence": decision.evidence,
                    "instruction": "Required outcome lacks executable support. Preserve the "
                    "source procedure and disclose the exact limitation. Use an explicit "
                    "source-consistent supported alternative only if it actually achieves "
                    "the prepared goal. Do not substitute item custody for restoration, "
                    "claim unsupported spell/item execution, or silently mark the finale "
                    "complete. If no supported alternative exists, retain the material gap.",
                }
            )
    return findings


def require_compatibility_review(store, compiled):
    """New compiled artifacts with gaps need a receipt bound to the exact assembly."""
    if not compiled.get("capability_findings"):
        return
    if compiled.get("compatibility_policy") is None:
        publication = store.get("publication") or {}
        if publication.get("compiled_sha256") == digest(compiled):
            return
        raise NeedsAttention(
            "Unpublished legacy compilation requires the current capability policy."
        )
    signature = compatibility_signature(compiled)
    receipt = store.get("compatibility-review", signature)
    if not receipt or compatibility_findings(compiled, CompatibilityReview.model_validate(receipt)):
        raise NeedsAttention("Required capability compatibility review is incomplete or blocked.")
