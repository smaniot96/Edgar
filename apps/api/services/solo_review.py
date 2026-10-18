"""Spoiler-safe review of an adventure draft for a solo host (EDG-176).

The payload is an explicit allowlist built field by field from engine results. It carries fixed
copy, check status, counts per classification, coverage and warnings. It never carries passage
text, scene/entity/passage identifiers, planner or secret content, or draft JSON. Publication still
goes through the existing compile/save/publish routes and their validation: `reviewed_digest` is
only offered when every local check passes, and the publish route re-verifies it.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from edgar_core.manifests import AdventureManifest, ManifestError, manifest_digest
from edgar_core.prepared import validate_prepared_campaign
from ingestion.manifest import validate_spans

from . import adventures_meta as store
from .adventure_manifests import _bundle

logger = logging.getLogger(__name__)

SOLO_REVIEWER = "Solo host (spoiler-safe review)"

CheckCode = Literal[
    "draft_present",
    "draft_well_formed",
    "spans_match_source",
    "edition_matches_source",
    "passages_classified",
    "prepared_structure_valid",
    "opening_scene_reviewed",
]
WarningCode = Literal["rules_edition_unspecified", "no_entities_declared", "no_resolution_policies"]

# (passed copy, failed copy). Failed copy may name a count; it never names content.
_COPY: dict[str, tuple[str, str]] = {
    "draft_present": (
        "A review draft is prepared.",
        "No review draft is prepared for this adventure.",
    ),
    "draft_well_formed": (
        "The review draft has a valid structure.",
        "The review draft has an invalid structure and must be prepared again.",
    ),
    "spans_match_source": (
        "Every source span matches the retained extraction.",
        "The draft's source spans no longer match the retained extraction.",
    ),
    "edition_matches_source": (
        "The rules edition matches the source.",
        "The draft's rules edition differs from the source edition.",
    ),
    "passages_classified": (
        "Every passage has an explicit audience.",
        "{count} passage(s) still have no explicit audience.",
    ),
    "prepared_structure_valid": (
        "The prepared structure passes deterministic validation.",
        "The prepared structure failed {count} deterministic validation check(s).",
    ),
    "opening_scene_reviewed": (
        "The opening scene has unconditional reviewed player text.",
        "The opening scene has no unconditional reviewed player text.",
    ),
}
# Every automatic check is recovered by regenerating the draft; none asks the host to read secrets.
_RECOVERY: dict[str, str] = {code: "regenerate" for code in _COPY}
_WARNING_COPY: dict[str, str] = {
    "rules_edition_unspecified": "The source does not declare a rules edition.",
    "no_entities_declared": "The draft declares no actors, clues, hazards or endings.",
    "no_resolution_policies": "The draft declares no resolution policies.",
}


class SafeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SoloCheck(SafeRecord):
    code: CheckCode
    status: Literal["passed", "failed"]
    message: str = Field(max_length=300)
    count: int | None = None
    recovery: Literal["regenerate"] | None = None


class SoloWarning(SafeRecord):
    code: WarningCode
    message: str = Field(max_length=200)


class SoloCounts(SafeRecord):
    passages: int
    player: int
    planner: int
    excluded: int
    unreviewed: int
    scenes: int
    entities: int
    resolution_policies: int
    coverage_percent: int = Field(ge=0, le=100)


class SoloReview(SafeRecord):
    """Allowlisted spoiler-safe payload. Shared campaigns receive only `mode`."""

    mode: Literal["solo", "shared"]
    state: Literal["no_draft", "blocked", "ready", "published"] | None = None
    title: str | None = Field(default=None, max_length=200)
    rules_edition: str | None = Field(default=None, max_length=32)
    checks: list[SoloCheck] = Field(default_factory=list, max_length=12)
    warnings: list[SoloWarning] = Field(default_factory=list, max_length=8)
    counts: SoloCounts | None = None
    reviewed_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewer: str | None = None


def shared_review() -> SoloReview:
    return SoloReview(mode="shared")


def _check(code: str, ok: bool, count: int | None = None) -> SoloCheck:
    passed, failed = _COPY[code]
    if ok:
        return SoloCheck(code=code, status="passed", message=passed)
    message = failed.format(count=count) if count is not None else failed
    return SoloCheck(
        code=code, status="failed", message=message, count=count, recovery=_RECOVERY[code]
    )


def _coverage_percent(extraction, manifest: AdventureManifest) -> int:
    pages = dict(extraction.text_pages())
    total = sum(sum(1 for c in text if not c.isspace()) for text in pages.values())
    covered = sum(
        sum(1 for c in pages[p.page][p.start : p.end] if not c.isspace()) for p in manifest.passages
    )
    return min(100, round(100 * covered / total)) if total else 0


def _warnings(meta_edition: str, manifest: AdventureManifest) -> list[SoloWarning]:
    found: list[str] = []
    if meta_edition == "unspecified":
        found.append("rules_edition_unspecified")
    if not manifest.entities:
        found.append("no_entities_declared")
    if not manifest.resolution_policies:
        found.append("no_resolution_policies")
    return [SoloWarning(code=code, message=_WARNING_COPY[code]) for code in found]


def build_solo_review(slug: str) -> SoloReview:
    """Engine checks for a solo host. Raises the same ManifestError family as the manifest routes."""
    raw_meta = store.load_meta(slug)
    meta, extraction = _bundle(slug)
    title = str(raw_meta.get("title") or "").strip()[:200] or None
    edition = meta.get("rules_edition", "unspecified")
    draft = meta.get("manifest_draft")
    if draft is None:
        if meta.get("manifest"):
            return SoloReview(
                mode="solo",
                state="published",
                title=title,
                rules_edition=edition,
                reviewer=SOLO_REVIEWER,
            )
        return SoloReview(
            mode="solo",
            state="no_draft",
            title=title,
            rules_edition=edition,
            checks=[_check("draft_present", False)],
            reviewer=SOLO_REVIEWER,
        )

    checks = [_check("draft_present", True)]
    try:
        manifest = AdventureManifest.model_validate(draft)
    except (ValueError, TypeError):
        checks.append(_check("draft_well_formed", False))
        return SoloReview(
            mode="solo",
            state="blocked",
            title=title,
            rules_edition=edition,
            checks=checks,
            reviewer=SOLO_REVIEWER,
        )
    checks.append(_check("draft_well_formed", True))

    try:
        validate_spans(extraction, manifest)
        spans_ok = True
    except (ManifestError, ValueError):
        spans_ok = False
    checks.append(_check("spans_match_source", spans_ok))

    checks.append(_check("edition_matches_source", manifest.rules_edition == edition))

    audiences = Counter(p.audience for p in manifest.passages)
    unclassified = audiences["unreviewed"]
    checks.append(_check("passages_classified", unclassified == 0, unclassified or None))

    if manifest.prepared is None:
        checks.append(_check("prepared_structure_valid", True))
    else:
        try:
            findings = len(validate_prepared_campaign(manifest, manifest.prepared).findings)
        except (ValueError, TypeError, KeyError):
            findings = 1
        checks.append(_check("prepared_structure_valid", findings == 0, findings or None))

    opening = manifest.opening_scene_id
    opening_ok = bool(opening) and any(
        p.audience == "player" and opening in p.scene_ids and not p.requires
        for p in manifest.passages
    )
    checks.append(_check("opening_scene_reviewed", opening_ok))

    ready = all(check.status == "passed" for check in checks)
    counts = SoloCounts(
        passages=len(manifest.passages),
        player=audiences["player"],
        planner=audiences["planner"],
        excluded=audiences["excluded"],
        unreviewed=unclassified,
        scenes=len(manifest.scenes),
        entities=len(manifest.entities),
        resolution_policies=len(manifest.resolution_policies),
        coverage_percent=_coverage_percent(extraction, manifest) if spans_ok else 0,
    )
    return SoloReview(
        mode="solo",
        state="ready" if ready else "blocked",
        title=title,
        rules_edition=edition,
        checks=checks,
        warnings=_warnings(edition, manifest),
        counts=counts,
        reviewed_digest=manifest_digest(manifest) if ready else None,
        reviewer=SOLO_REVIEWER,
    )


def record_spoiler_review_opened(slug: str, campaign_id: int, user_id: int | None) -> None:
    """Audit line for the opt-in spoiler path. Records who opened what, never the material."""
    logger.info(
        "solo spoiler review opened adventure=%s campaign=%s user=%s", slug, campaign_id, user_id
    )
