"""Retained, evidence-backed repair of invalid mechanical profile page provenance."""

from copy import deepcopy
from typing import Literal

from pydantic import Field

from edgar_core.campaign_semantics import Record
from edgar_core.identifiers import Identifier

from .source import evidence_pages
from .store import NeedsAttention, digest

PROFILE_REFERENCE_POLICY = "profile-physical-page-membership-1"


class ProfileCorrectionRejected(NeedsAttention):
    pass


class ProfilePageJoin(Record):
    owner: str = Field(description="shared, or the exact chapter_id owning the profile")
    expected_id: Identifier
    mode: Literal["catalogue", "source"]
    source_pages: list[int]
    evidence_quote: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class ProfilePageJoins(Record):
    joins: list[ProfilePageJoin]


def profile_reference_defects(bundles, source):
    if source["kind"] != "pdf":
        return []
    known = {page["page"] for page in source["pages"]}
    owners = [
        ("shared", bundles.get("mechanical_profiles", [])),
        *[
            (chapter["chapter_id"], chapter.get("mechanical_profiles", []))
            for chapter in bundles["chapters"]
        ],
    ]
    return [
        {
            "owner": owner,
            "expected_id": row["id"],
            "profile": row,
            "invalid_source_pages": sorted(set(row["source_pages"]) - known),
        }
        for owner, profiles in owners
        for row in profiles
        if set(row["source_pages"]) - known
    ]


async def reconcile_profile_pages(workflow, spine, bundles):
    source = workflow.store.get("source")
    defects = profile_reference_defects(bundles, source)
    if not defects:
        return bundles
    names = {d["profile"]["creature"].casefold() for d in defects}
    catalogue = [
        row for row in workflow.store.get("mechanics") or [] if row["name"].casefold() in names
    ]
    signature = digest([PROFILE_REFERENCE_POLICY, bundles, source, catalogue])
    key = "profile-page-repair-" + signature
    receipt = workflow.store.get(key) or {}
    if receipt.get("state") == "rejected":
        raise ProfileCorrectionRejected(
            "Retained profile page correction needs attention: " + receipt["error"]
        )
    known = {p["page"] for p in source["pages"]}
    pages = sorted({p for d in defects for p in d["profile"]["source_pages"] if p in known})
    if not pages:
        pages = sorted({p for chapter in spine["chapters"] for p in chapter["pages"]})
    evidence = {
        "policy": PROFILE_REFERENCE_POLICY,
        "defects": defects,
        "physical_pages": sorted(known),
        "source_evidence": evidence_pages(source, pages),
        "verified_catalogue": catalogue,
        "chapter_ids": [chapter["id"] for chapter in spine["chapters"]],
    }
    raw = receipt.get("candidate")
    if raw is None:
        value = await workflow.semantic.call(
            "profile-page-joins-" + signature,
            "profile-page-joins",
            ProfilePageJoins,
            evidence,
            workflow.source_images(pages),
        )
        raw = value.model_dump()
        receipt = {
            "policy": PROFILE_REFERENCE_POLICY,
            "candidate": raw,
            "original_bundles_sha256": digest(bundles),
            "defects": defects,
        }
        workflow.store.put(key, receipt)
    candidate = deepcopy(bundles)
    owners = {
        "shared": candidate.get("mechanical_profiles", []),
        **{b["chapter_id"]: b.get("mechanical_profiles", []) for b in candidate["chapters"]},
    }
    allowed = {(d["owner"], d["expected_id"]): d for d in defects}
    actor_provenance = []
    try:
        seen = set()
        for join in ProfilePageJoins.model_validate(raw).joins:
            identity = join.owner, join.expected_id
            if identity not in allowed or identity in seen:
                raise NeedsAttention(
                    "Profile correction targeted an unaffected or repeated profile."
                )
            seen.add(identity)
            original = allowed[identity]["profile"]
            row = next(p for p in owners[join.owner] if p["id"] == join.expected_id)
            cited = set(join.source_pages)
            if not cited <= known or not (set(original["source_pages"]) & known) <= cited:
                raise NeedsAttention(
                    "Profile correction must retain valid source pages and cite existing physical pages."
                )
            if join.mode == "catalogue":
                verified = [
                    r for r in catalogue if r["name"].casefold() == row["creature"].casefold()
                ]
                if not any(join.evidence_quote in r["text"] for r in verified):
                    raise NeedsAttention(
                        "Catalogue rebinding requires an exact existing verified template quote."
                    )
                row["source_stat_block"] = ""
                note = "uses the verified catalogue template " + row["creature"]
            else:
                text = "\n".join(p["text"] for p in source["pages"] if p["page"] in cited)
                if not cited or join.evidence_quote not in text:
                    raise NeedsAttention(
                        "Source profile correction requires an exact quote from its actual cited PDF pages."
                    )
                note = "retains its source stat block with corrected physical-page provenance"
            row["source_pages"] = list(dict.fromkeys(join.source_pages))
            bound_actors = {
                appearance["actor_id"]
                for chapter in candidate["chapters"]
                for event in chapter["events"]
                for appearance in event["encounters"]
                if appearance.get("profile_id") == row["id"]
            }
            for chapter in candidate["chapters"]:
                for actor in chapter["new_entities"]:
                    origin = actor["origin"]
                    invalid = set(origin["pages"]) - known
                    # Retained actors for this exact profile may copy its external
                    # locator. Preserve valid citations, identity and all prose.
                    if (
                        actor["id"] in bound_actors
                        and invalid
                        and invalid <= set(allowed[identity]["invalid_source_pages"])
                    ):
                        before = list(origin["pages"])
                        origin["pages"] = [p for p in before if p in known]
                        origin["note"] += (
                            "\n\nOriginal non-PDF locators copied from the bound profile: "
                            + str(sorted(invalid))
                            + ". These are not verified physical pages of this PDF."
                        )
                        actor_provenance.append(
                            {
                                "actor_id": actor["id"],
                                "profile_id": row["id"],
                                "original_pages": before,
                                "physical_pages": origin["pages"],
                                "retained_locator_labels": sorted(invalid),
                            }
                        )
            # Keep an explicit audit trail in both receipt and the owning running
            # material. Never silently convert an external locator into a PDF page.
            for chapter in candidate["chapters"]:
                if join.owner == chapter["chapter_id"] or any(
                    e.get("profile_id") == row["id"]
                    for event in chapter["events"]
                    for e in event["encounters"]
                ):
                    chapter["source_notes"] += (
                        f"\n\nProfile {row['id']} {note}. Original invalid physical-page "
                        f"locators {allowed[identity]['invalid_source_pages']} remain in the retained "
                        "profile correction receipt; they are not verified pages of this PDF. "
                        + join.rationale
                    )
        if seen != allowed.keys() or profile_reference_defects(candidate, source):
            raise NeedsAttention("Profile page correction left invalid provenance unresolved.")
    except (ValueError, KeyError, StopIteration) as exc:
        workflow.store.put(key, {**receipt, "state": "rejected", "error": str(exc)})
        raise ProfileCorrectionRejected(
            "Profile page correction was rejected: " + str(exc)
        ) from exc
    workflow.store.put(
        key,
        {
            **receipt,
            "state": "accepted",
            "result_bundles_sha256": digest(candidate),
            "matching_actor_provenance": actor_provenance,
        },
    )
    return candidate
