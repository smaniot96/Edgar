"""Typed, version-bound evidence for private campaign preparation (EDG-019).

This module deliberately contains no retrieval, embedding, provider, or database code. It
defines immutable snapshots and deterministic checks. API adapters read exact records and the
rules engine parses creature statistics; the authoring model may select names and quantities,
but cannot supply certified statistics, XP, difficulty, or a ready result.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from edgar_core.identifiers import Digest, Identifier

BookRole = Literal["phb", "dmg", "mm"]
RulesEdition = Literal["2014", "2024"]
Difficulty = Literal["trivial", "easy", "medium", "hard", "deadly"]
MechanicCategory = Literal["action", "resource", "rest", "progression", "treasure", "hazard"]


class GroundingRecord(BaseModel):
    """Strict base for private, source-bound grounding records."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class BookBinding(GroundingRecord):
    """Commission-frozen identity of one required PHB, DMG, or MM source."""

    role: BookRole
    collection: Identifier
    source: str = Field(min_length=1, max_length=500)
    source_sha256: Digest
    extraction_sha256: Digest
    rules_edition: RulesEdition


class BookTextEvidence(GroundingRecord):
    """One exact indexed excerpt retained with physical page and text digest."""

    page: int = Field(ge=1)
    page_end: int = Field(ge=1)
    section: str = Field(min_length=1, max_length=1000)
    text: str = Field(min_length=1, max_length=30000)
    text_sha256: Digest
    rule_policy: Literal["core", "optional", "variant", "unclassified"] = "unclassified"

    @model_validator(mode="after")
    def page_order(self) -> BookTextEvidence:
        if self.page_end < self.page:
            raise ValueError("Evidence page_end must be at or after page.")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("Book evidence text does not match its text digest.")
        return self


class VerifiedBookEvidence(GroundingRecord):
    """Exact active-index evidence for one frozen rulebook identity."""

    binding: BookBinding
    index_version: Digest
    chunks: list[BookTextEvidence] = Field(min_length=1, max_length=20000)
    text_sha256: Digest

    @model_validator(mode="after")
    def lineage_and_text_match(self) -> VerifiedBookEvidence:
        rows = sorted(self.chunks, key=lambda row: (row.page, row.page_end, row.section, row.text_sha256))
        expected = hashlib.sha256(
            "\n\n".join(row.text for row in rows).encode("utf-8")
        ).hexdigest()
        if expected != self.text_sha256:
            raise ValueError("Book snapshot text does not match its text digest.")
        return self

    @property
    def evidence_sha256(self) -> str:
        return _canonical_digest(self.model_dump(mode="json"))


class VerifiedBookSnapshot(GroundingRecord):
    """All required PHB/DMG/MM records, pinned to one declared edition."""

    rules_edition: RulesEdition
    books: list[VerifiedBookEvidence] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def exactly_required_books(self) -> VerifiedBookSnapshot:
        roles = [book.binding.role for book in self.books]
        if set(roles) != {"phb", "dmg", "mm"} or len(roles) != 3:
            raise ValueError("A verified snapshot must contain exactly one PHB, DMG, and MM.")
        if any(book.binding.rules_edition != self.rules_edition for book in self.books):
            raise ValueError("Required book editions must exactly match the snapshot edition.")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return _canonical_digest(self.model_dump(mode="json"))

    def for_role(self, role: BookRole) -> VerifiedBookEvidence:
        return next(book for book in self.books if book.binding.role == role)


class BookReferenceRequest(GroundingRecord):
    """A study-stage request for named references; selection never embeds or broad-searches."""

    role: BookRole
    references: list[str] = Field(min_length=1, max_length=100)


class BookStudyEvidence(BookTextEvidence):
    """An excerpt carries its own exact book identity even across equal page numbers."""

    book_binding: BookBinding
    index_version: Digest


class BookStudyReceipt(GroundingRecord):
    """Versioned, exact excerpts supplied to the private P02 study stage."""

    book_snapshot_sha256: Digest
    rules_edition: RulesEdition
    requested_references: list[BookReferenceRequest]
    evidence: list[BookStudyEvidence] = Field(min_length=1, max_length=3000)
    missing_references: list[str] = Field(default_factory=list, max_length=300)

    @property
    def valid(self) -> bool:
        return not self.missing_references

    @property
    def study_sha256(self) -> str:
        return _canonical_digest(self.model_dump(mode="json"))


class HeroSnapshot(GroundingRecord):
    """Actual character state captured before provider work starts."""

    character_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=255)
    character_class: str = Field(min_length=1, max_length=255)
    level: int = Field(ge=1, le=20)
    hp_max: int = Field(ge=1)
    stats: dict[str, Any] = Field(default_factory=dict, max_length=200)
    inventory: dict[str, Any] = Field(default_factory=dict, max_length=200)


class HeroPartySnapshot(GroundingRecord):
    """Actual commissioning roster, including exact sheet and inventory data."""

    heroes: list[HeroSnapshot] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_characters(self) -> HeroPartySnapshot:
        ids = [hero.character_id for hero in self.heroes]
        if len(ids) != len(set(ids)):
            raise ValueError("Hero snapshot cannot repeat a character ID.")
        return self

    @property
    def party_sha256(self) -> str:
        canonical = sorted(
            (hero.model_dump(mode="json") for hero in self.heroes),
            key=lambda hero: hero["character_id"],
        )
        return _canonical_digest(canonical)


class EncounterCreatureRequest(GroundingRecord):
    """An author may choose an exact name, count, and role, never its game statistics."""

    creature_name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1, le=100)
    role: Literal["boss", "foe"] = "foe"
    actor_id: Identifier | None = None


class MechanicRequirement(GroundingRecord):
    """Mechanic claimed by authored content, classified for fail-closed support checks."""

    category: MechanicCategory
    mechanic_id: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class EncounterPlan(GroundingRecord):
    """P05 encounter request with explicit scene, threat ceiling, and mechanic claims."""

    encounter_id: Identifier
    scene_id: Identifier
    target_level: int | None = Field(default=None, ge=1, le=20)
    creatures: list[EncounterCreatureRequest] = Field(min_length=1, max_length=100)
    maximum_difficulty: Difficulty
    mechanics: list[MechanicRequirement] = Field(default_factory=list, max_length=200)
    actor_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def no_duplicate_creature_names(self) -> EncounterPlan:
        normalized = [(" ".join(row.creature_name.casefold().split()), row.actor_id)
                      for row in self.creatures]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Group repeated unnamed creatures into one quantity row; named actors need distinct actor IDs.")
        actors = [row.actor_id for row in self.creatures if row.actor_id is not None]
        if len(actors) != len(set(actors)):
            raise ValueError("A canonical actor can occupy only one encounter slot.")
        if any(row.actor_id is not None and row.quantity != 1 for row in self.creatures):
            raise ValueError("A named actor request must have quantity one.")
        return self

    @property
    def plan_sha256(self) -> str:
        return _canonical_digest(self.model_dump(mode="json"))


class CreatureSourceRegion(GroundingRecord):
    bounds: list[float] = Field(min_length=4, max_length=4)
    text_sha256: Digest
    selected_start: int = Field(ge=0)
    selected_end: int = Field(ge=1)
    reviewed_suffix: str | None = None
    reviewed_prefix: str | None = None

    @model_validator(mode="after")
    def valid_region(self) -> CreatureSourceRegion:
        left, top, right, bottom = self.bounds
        if not all(math.isfinite(value) for value in self.bounds) or left < 0 or top < 0 or right <= left or bottom <= top:
            raise ValueError("Supplemental creature region needs ordered page bounds.")
        if self.selected_end <= self.selected_start:
            raise ValueError("Supplemental creature region needs a nonempty exact text selection.")
        return self


class CreatureSourceEvidence(GroundingRecord):
    """Supplemental exact PDF proof; this text was not supplied by the active index."""

    pdf_sha256: Digest
    physical_page: int = Field(ge=1)
    bounds: list[float] = Field(min_length=4, max_length=4)
    regions: list[CreatureSourceRegion] = Field(min_length=1)
    parser: str = Field(min_length=1, max_length=200)
    parser_version: str = Field(min_length=1, max_length=100)
    page_text_sha256: Digest
    extracted_text_sha256: Digest
    record_text_sha256: Digest
    reviewed_labels: list[str] = Field(default_factory=list, max_length=2)
    reviewed_by: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_bounds(self) -> CreatureSourceEvidence:
        left, top, right, bottom = self.bounds
        if not all(math.isfinite(value) for value in self.bounds) or left < 0 or top < 0 or right <= left or bottom <= top:
            raise ValueError("Supplemental creature evidence needs exact ordered page bounds.")
        if self.bounds != [min(r.bounds[0] for r in self.regions), min(r.bounds[1] for r in self.regions),
                           max(r.bounds[2] for r in self.regions), max(r.bounds[3] for r in self.regions)]:
            raise ValueError("Supplemental creature bounds must enclose exactly its reviewed regions.")
        return self


class CreatureTextCorrection(GroundingRecord):
    """A source-checksum-bound glyph explicitly read from the printed PDF."""

    start: int = Field(ge=0)
    end: int = Field(ge=1)
    original: str = Field(min_length=1)
    reviewed: str = Field(min_length=1)
    physical_page: int = Field(ge=1)
    bounds: list[float] = Field(min_length=4, max_length=4)
    reviewed_by: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_correction(self) -> CreatureTextCorrection:
        left, top, right, bottom = self.bounds
        if (self.end - self.start != len(self.original)
                or not all(math.isfinite(v) for v in self.bounds)
                or left < 0 or top < 0 or right <= left or bottom <= top):
            raise ValueError("A reviewed source glyph needs exact original offsets and PDF bounds.")
        return self


class ModuleCreatureEvidence(GroundingRecord):
    """One exact prepared-module passage from the verified frozen extraction."""

    source_sha256: Digest
    extraction_sha256: Digest
    actor_id: Identifier
    passage_id: Identifier
    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    text_sha256: Digest
    parsing_text_sha256: Digest | None = None
    source_corrections: list[CreatureTextCorrection] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_span(self) -> ModuleCreatureEvidence:
        if self.end <= self.start:
            raise ValueError("Prepared module creature evidence needs a nonempty exact span.")
        return self


class GroundedCreature(GroundingRecord):
    """A selected MM record resolved and parsed by repository code."""

    creature_id: Digest
    creature_name: str = Field(min_length=1, max_length=200)
    collection: Identifier
    source: str = Field(min_length=1, max_length=500)
    source_sha256: Digest
    extraction_sha256: Digest
    rules_edition: RulesEdition
    index_version: Digest | None
    page: int = Field(ge=1)
    page_end: int = Field(ge=1)
    text_sha256: Digest
    stat_block_text: str = Field(min_length=1, max_length=30000)
    challenge_rating: str = Field(min_length=1, max_length=10)
    xp: int = Field(ge=0)
    monster_stats: dict[str, Any] = Field(min_length=1, max_length=100)
    evidence_origin: Literal["active_index", "source_pdf", "prepared_module"] = "active_index"
    source_evidence: CreatureSourceEvidence | None = None
    module_evidence: ModuleCreatureEvidence | None = None

    @model_validator(mode="after")
    def citation_and_text_match(self) -> GroundedCreature:
        if self.page_end < self.page:
            raise ValueError("Creature citation page_end must be at or after page.")
        if hashlib.sha256(self.stat_block_text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("Creature stat text does not match its text digest.")
        if self.evidence_origin == "source_pdf":
            proof = self.source_evidence
            if (
                proof is None or proof.pdf_sha256 != self.source_sha256
                or proof.physical_page != self.page or self.page_end != self.page
                or proof.record_text_sha256 != self.text_sha256
            ):
                raise ValueError("Supplemental creature text needs its matching frozen PDF proof.")
            if self.index_version is None or self.module_evidence is not None:
                raise ValueError("Supplemental PDF proof needs the frozen index binding and no module proof.")
        elif self.evidence_origin == "prepared_module":
            proof = self.module_evidence
            if (
                proof is None or self.index_version is not None or self.source_evidence is not None
                or proof.source_sha256 != self.source_sha256
                or proof.extraction_sha256 != self.extraction_sha256
                or proof.page != self.page or self.page_end != self.page
                or proof.text_sha256 != self.text_sha256
                or proof.end - proof.start != len(self.stat_block_text)
            ):
                raise ValueError("Prepared module creature text needs its exact frozen passage proof.")
            parsed_text = self.stat_block_text
            previous_end = 0
            for correction in proof.source_corrections:
                if (correction.physical_page != self.page or correction.start < previous_end
                        or self.stat_block_text[correction.start:correction.end] != correction.original):
                    raise ValueError("Reviewed creature glyphs must match nonoverlapping original source spans.")
                previous_end = correction.end
            for correction in reversed(proof.source_corrections):
                parsed_text = parsed_text[:correction.start] + correction.reviewed + parsed_text[correction.end:]
            if proof.source_corrections:
                if hashlib.sha256(parsed_text.encode("utf-8")).hexdigest() != proof.parsing_text_sha256:
                    raise ValueError("Reviewed creature parsing text must match its recorded digest.")
            elif proof.parsing_text_sha256 is not None:
                raise ValueError("A parsing-text digest requires explicit reviewed source corrections.")
        elif self.source_evidence is not None:
            raise ValueError("Active-index creature text cannot claim supplemental PDF proof.")
        elif self.index_version is None or self.module_evidence is not None:
            raise ValueError("Active-index creature text needs its index version and no module proof.")
        return self


class GroundedEncounterMember(GroundingRecord):
    creature: GroundedCreature
    actor_id: Identifier | None = None
    requested_quantity: int = Field(ge=1, le=100)
    effective_quantity: int = Field(ge=0, le=100)
    role: Literal["boss", "foe"]


class EncounterThresholds(GroundingRecord):
    easy: int = Field(ge=0)
    medium: int = Field(ge=0)
    hard: int = Field(ge=0)
    deadly: int = Field(ge=0)


class SourceEncounterGroundingReceipt(GroundingRecord):
    """Exact source roster and supported mechanics, before playthrough adaptation."""

    grounding_scope: Literal["source"] = "source"
    encounter_id: Identifier
    scene_id: Identifier
    plan_sha256: Digest
    book_snapshot_sha256: Digest
    rules_edition: RulesEdition
    engine_edition_basis: Literal["2014"] = "2014"
    creatures: list[GroundedEncounterMember] = Field(min_length=1, max_length=100)
    raw_xp: int = Field(ge=0)
    adaptation_notes: list[str] = Field(default_factory=list, max_length=100)
    supported_mechanics: list[MechanicRequirement] = Field(default_factory=list, max_length=200)
    unsupported_mechanics: list[MechanicRequirement] = Field(default_factory=list, max_length=200)
    findings: list[str] = Field(default_factory=list, max_length=500)
    actor_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def preserve_source_roster(self) -> SourceEncounterGroundingReceipt:
        if any(row.effective_quantity != row.requested_quantity for row in self.creatures):
            raise ValueError("Source preparation must preserve every requested creature.")
        if self.raw_xp != sum(row.creature.xp * row.requested_quantity for row in self.creatures):
            raise ValueError("Source encounter XP must match its exact original roster.")
        return self

    @property
    def valid(self) -> bool:
        return not self.findings and not self.unsupported_mechanics

    @property
    def receipt_sha256(self) -> str:
        return _canonical_digest(self.model_dump(mode="json"))


class EncounterGroundingReceipt(GroundingRecord):
    """Engine-owned result for a single encounter and exact source/hero inputs."""

    encounter_id: Identifier
    scene_id: Identifier
    plan_sha256: Digest
    book_snapshot_sha256: Digest
    hero_party_sha256: Digest
    rules_edition: RulesEdition
    engine_edition_basis: Literal["2014"] = "2014"
    creatures: list[GroundedEncounterMember] = Field(min_length=1, max_length=100)
    raw_xp: int = Field(ge=0)
    adjusted_xp: int = Field(ge=0)
    difficulty: Difficulty
    thresholds: EncounterThresholds
    monster_count_multiplier: float = Field(gt=0, le=5)
    solo_budget: dict[str, Any] | None = None
    adaptation_notes: list[str] = Field(default_factory=list, max_length=100)
    supported_mechanics: list[MechanicRequirement] = Field(default_factory=list, max_length=200)
    unsupported_mechanics: list[MechanicRequirement] = Field(default_factory=list, max_length=200)
    findings: list[str] = Field(default_factory=list, max_length=500)
    actor_ids: list[Identifier] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.findings and not self.unsupported_mechanics

    @property
    def receipt_sha256(self) -> str:
        return _canonical_digest(self.model_dump(mode="json"))


_CR_XP = {
    "0": 10,
    "1/8": 25,
    "1/4": 50,
    "1/2": 100,
    "1": 200,
    "2": 450,
    "3": 700,
    "4": 1100,
    "5": 1800,
    "6": 2300,
    "7": 2900,
    "8": 3900,
    "9": 5000,
    "10": 5900,
    "11": 7200,
    "12": 8400,
    "13": 10000,
    "14": 11500,
    "15": 13000,
    "16": 15000,
    "17": 18000,
    "18": 20000,
    "19": 22000,
    "20": 25000,
    "21": 33000,
    "22": 41000,
    "23": 50000,
    "24": 62000,
    "25": 75000,
    "26": 90000,
    "27": 105000,
    "28": 120000,
    "29": 135000,
    "30": 155000,
}
_CHALLENGE_LINE = re.compile(
    r"^Challenge\s+(\d+(?:/\d+)?)(?:\s+\((\d{1,3}(?:\s*,\s*\d{3})+|\d+)\s+XP\))?\s*$", re.IGNORECASE
)


def challenge_xp_from_stat_block(text: str) -> tuple[str, int]:
    """Read and cross-check the printed CR/XP line; never accept model-supplied numbers."""
    matches = [
        _CHALLENGE_LINE.fullmatch(line.strip().strip("*_`"))
        for line in text.splitlines()
        if line.strip()
    ]
    rows = [match for match in matches if match is not None]
    if len(rows) != 1:
        raise ValueError("Exact creature record must contain one complete Challenge/XP line.")
    rating = rows[0].group(1)
    printed_xp = rows[0].group(2)
    expected_xp = _CR_XP.get(rating)
    if expected_xp is None or printed_xp is None:
        raise ValueError("Creature CR/XP is missing or outside the engine's 2014 table.")
    if int(re.sub(r"[\s,]", "", printed_xp)) != expected_xp:
        raise ValueError("Printed XP conflicts with the engine-owned 2014 CR/XP table.")
    return rating, expected_xp


# 2014 DMG adventuring difficulty thresholds by character level: easy, medium, hard, deadly.
_XP_THRESHOLDS = {
    1: (25, 50, 75, 100), 2: (50, 100, 150, 200), 3: (75, 150, 225, 400),
    4: (125, 250, 375, 500), 5: (250, 500, 750, 1100), 6: (300, 600, 900, 1400),
    7: (350, 750, 1100, 1700), 8: (450, 900, 1400, 2100), 9: (550, 1100, 1600, 2400),
    10: (600, 1200, 1900, 2800), 11: (800, 1600, 2400, 3600), 12: (1000, 2000, 3000, 4500),
    13: (1100, 2200, 3400, 5100), 14: (1250, 2500, 3800, 5700), 15: (1400, 2800, 4300, 6400),
    16: (1600, 3200, 4800, 7200), 17: (2000, 3900, 5900, 8800), 18: (2100, 4200, 6300, 9500),
    19: (2400, 4900, 7300, 10900), 20: (2800, 5700, 8500, 12700),
}
_DIFFICULTY_ORDER = {name: i for i, name in enumerate(("trivial", "easy", "medium", "hard", "deadly"))}
_MULTIPLIERS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)


def encounter_thresholds(party: HeroPartySnapshot) -> EncounterThresholds:
    """Sum the 2014 DMG per-hero XP budgets for the actual captured roster."""
    totals = [0, 0, 0, 0]
    for hero in party.heroes:
        for index, value in enumerate(_XP_THRESHOLDS[hero.level]):
            totals[index] += value
    return EncounterThresholds(easy=totals[0], medium=totals[1], hard=totals[2], deadly=totals[3])


def encounter_multiplier(monster_count: int, party_size: int) -> float:
    """2014 DMG monster-count multiplier with small/large party adjustment."""
    if monster_count < 1 or party_size < 1:
        raise ValueError("An encounter and party must each contain at least one member.")
    index = (
        1 if monster_count == 1 else
        2 if monster_count == 2 else
        3 if monster_count <= 6 else
        4 if monster_count <= 10 else
        5 if monster_count <= 14 else 6
    )
    if party_size <= 2:
        index = min(index + 1, len(_MULTIPLIERS) - 1)
    elif party_size >= 6:
        index = max(index - 1, 0)
    return _MULTIPLIERS[index]


def encounter_difficulty(adjusted_xp: int, thresholds: EncounterThresholds) -> Difficulty:
    """Classify adjusted encounter XP against the real roster's summed XP thresholds."""
    if adjusted_xp < thresholds.easy:
        return "trivial"
    if adjusted_xp < thresholds.medium:
        return "easy"
    if adjusted_xp < thresholds.hard:
        return "medium"
    if adjusted_xp < thresholds.deadly:
        return "hard"
    return "deadly"


def difficulty_exceeds(actual: Difficulty, maximum: Difficulty) -> bool:
    return _DIFFICULTY_ORDER[actual] > _DIFFICULTY_ORDER[maximum]


def select_book_evidence(
    snapshot: VerifiedBookSnapshot,
    requests: Sequence[BookReferenceRequest],
) -> BookStudyReceipt:
    """Select exact headings from a frozen snapshot with conservative normalized equality."""
    def norm(value: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())

    selected: dict[tuple[str, str], BookStudyEvidence] = {}
    missing: list[str] = []
    for request in requests:
        book = snapshot.for_role(request.role)
        by_section: dict[str, list[BookTextEvidence]] = {}
        for chunk in book.chunks:
            if chunk.rule_policy != "core":
                continue
            by_section.setdefault(norm(chunk.section), []).append(chunk)
        for reference in request.references:
            key = norm(reference)
            matches = by_section.get(key, [])
            label = f"{request.role}:{reference}"
            if not matches:
                missing.append(label)
                continue
            for match in matches:
                selected[(book.binding.role, match.text_sha256)] = BookStudyEvidence(
                    **match.model_dump(), book_binding=book.binding, index_version=book.index_version
                )
    return BookStudyReceipt(
        book_snapshot_sha256=snapshot.snapshot_sha256,
        rules_edition=snapshot.rules_edition,
        requested_references=list(requests),
        evidence=sorted(selected.values(), key=lambda item: (item.page, item.section, item.text_sha256)),
        missing_references=missing,
    )


def validate_mechanic_requirements(
    requirements: Sequence[MechanicRequirement], supported_mechanic_ids: set[str]
) -> tuple[list[MechanicRequirement], list[MechanicRequirement]]:
    """Split requested mechanics using the live engine-supported contract provided by adapter."""
    supported: dict[tuple[str, str], MechanicRequirement] = {}
    unsupported: dict[tuple[str, str], MechanicRequirement] = {}
    for requirement in requirements:
        key = (requirement.category, requirement.mechanic_id)
        if requirement.mechanic_id in supported_mechanic_ids:
            supported[key] = requirement
        else:
            unsupported[key] = requirement
    return list(supported.values()), list(unsupported.values())


__all__ = [
    "BookBinding", "BookReferenceRequest", "BookRole", "BookStudyReceipt", "BookTextEvidence",
    "Difficulty", "EncounterCreatureRequest", "EncounterGroundingReceipt", "SourceEncounterGroundingReceipt", "EncounterPlan",
    "EncounterThresholds", "CreatureSourceRegion", "CreatureSourceEvidence", "CreatureTextCorrection", "ModuleCreatureEvidence",
    "GroundedCreature", "GroundedEncounterMember", "HeroPartySnapshot",
    "HeroSnapshot", "MechanicCategory", "MechanicRequirement", "RulesEdition", "VerifiedBookEvidence",
    "VerifiedBookSnapshot", "challenge_xp_from_stat_block", "difficulty_exceeds", "encounter_difficulty",
    "encounter_multiplier", "encounter_thresholds", "select_book_evidence", "validate_mechanic_requirements",
]
