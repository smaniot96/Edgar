"""Read-only verified book and hero grounding adapters for private preparation (EDG-019).

Opening and reading a book is a payload-only Qdrant scroll under the active-index snapshot
lock. It never embeds, searches by vector, invokes a provider, or writes the source index.
Encounter preparation resolves exact MM identities, parses the retained stat text with the
existing rules engine, and derives every number from code-owned rules.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import structlog
from qdrant_client.models import Condition, FieldCondition, Filter, MatchValue

from agent.encounter_budget import apply_solo_budget
from agent.monster_stats import MonsterStatsError, parse_monster_stats
from agent.supported_rules import SUPPORTED_MECHANICS
from db.vector.client import get_qdrant_client
from db.vector.creatures import CreatureResolutionError, resolve_creature
from db.vector.retrieval import rule_policy
from db.vector.versions import active_index_read
from edgar_core.grounding import (
    BookBinding,
    BookReferenceRequest,
    BookStudyReceipt,
    BookTextEvidence,
    EncounterCreatureRequest,
    EncounterGroundingReceipt,
    EncounterPlan,
    GroundedCreature,
    GroundedEncounterMember,
    HeroPartySnapshot,
    HeroSnapshot,
    RulesEdition,
    VerifiedBookEvidence,
    VerifiedBookSnapshot,
    challenge_xp_from_stat_block,
    difficulty_exceeds,
    encounter_difficulty,
    encounter_multiplier,
    encounter_thresholds,
    select_book_evidence,
    validate_mechanic_requirements,
)
from edgar_core.manifests import ManifestError

log = structlog.get_logger(__name__)
# Share of a book's active records that may be uncitable (blank, or no exact page or heading,
# typically OCR damage) and be left out of verified evidence. They can never be cited; more than
# this means the index itself is unreliable and the book stays unavailable.
UNCITABLE_RECORD_BUDGET = 0.01


class GroundingUnavailable(ValueError):
    """Required exact evidence is absent, changed, ambiguous, or mechanically unreadable."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def snapshot_heroes(rows: Sequence[Mapping[str, Any]]) -> HeroPartySnapshot:
    """Freeze actual character fields from the pre-provider commissioning snapshot."""
    heroes = []
    for row in rows:
        try:
            heroes.append(
                HeroSnapshot(
                    character_id=int(row.get("character_id", row.get("id"))),
                    name=str(row["name"]),
                    character_class=str(row["character_class"]),
                    level=int(row["level"]),
                    hp_max=int(row["hp_max"]),
                    stats=dict(row.get("stats") or {}),
                    inventory=dict(row.get("inventory") or {}),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GroundingUnavailable(
                "hero_snapshot_invalid", "Actual party data is incomplete or malformed."
            ) from exc
    try:
        return HeroPartySnapshot(heroes=heroes)
    except ValueError as exc:
        raise GroundingUnavailable("hero_snapshot_invalid", str(exc)) from exc


def read_verified_book_snapshot(
    bindings: Sequence[BookBinding],
    *,
    rules_edition: RulesEdition,
    client: Any = None,
) -> VerifiedBookSnapshot:
    """Read exact PHB/DMG/MM active payloads without embeddings or provider work.

    Every source must match the identity frozen at commission. The full active payload count,
    source hash, extraction hash, and edition are checked while the existing index read lock is
    held. Legacy or unspecified editions cannot be promoted to verified evidence.
    """
    if rules_edition not in ("2014", "2024"):
        raise GroundingUnavailable("edition_unverified", "Choose a supported explicit rules edition.")
    by_role = {binding.role: binding for binding in bindings}
    if set(by_role) != {"phb", "dmg", "mm"} or len(bindings) != 3:
        raise GroundingUnavailable("required_books_missing", "PHB, DMG, and MM bindings are required.")
    if any(binding.rules_edition != rules_edition for binding in bindings):
        raise GroundingUnavailable("edition_mismatch", "All required books must match the selected edition.")
    role_collections = {
        "phb": {"rules_player_handbook", "player_handbook"},
        "dmg": {"rules_dm_guide", "dm_guide"},
        "mm": {"rules_monster_manual", "monster_manual"},
    }
    if any(binding.collection not in role_collections[role] for role, binding in by_role.items()):
        raise GroundingUnavailable("book_role_mismatch", "A required book is bound to the wrong source collection.")
    identities = [
        (binding.source, binding.source_sha256, binding.extraction_sha256)
        for binding in bindings
    ]
    if len(identities) != len(set(identities)):
        raise GroundingUnavailable("book_identity_collision", "PHB, DMG, and MM need separate exact source identities.")

    qdrant = client if client is not None else get_qdrant_client()
    books: list[VerifiedBookEvidence] = []
    for role in ("phb", "dmg", "mm"):
        binding = by_role[role]
        if not qdrant.collection_exists(binding.collection):
            raise GroundingUnavailable("required_book_unavailable", f"The required {role.upper()} source is unavailable.")

        state: dict[str, Any] = {}
        text_rows: list[dict[str, Any]] = []
        uncitable: Counter[str] = Counter()
        with active_index_read(qdrant, binding.collection, state_out=state) as active_filter:
            matching_active = []
            for source, entry in (state.get("sources") or {}).items():
                receipt = entry.get("active") or {}
                if (
                    source == binding.source
                    and receipt.get("source_sha256") == binding.source_sha256
                    and receipt.get("extraction_sha256") == binding.extraction_sha256
                    and receipt.get("rules_edition") == binding.rules_edition
                ):
                    matching_active.append(receipt)
            if len(matching_active) != 1:
                raise GroundingUnavailable(
                    "book_version_unavailable", f"The required {role.upper()} version is not active."
                )
            receipt = matching_active[0]
            index_version = receipt.get("index_version")
            if not isinstance(index_version, str) or len(index_version) != 64:
                raise GroundingUnavailable("book_version_unavailable", f"The {role.upper()} index has no stable version.")

            conditions: list[Condition] = [
                FieldCondition(key="source", match=MatchValue(value=binding.source)),
                FieldCondition(key="source_sha256", match=MatchValue(value=binding.source_sha256)),
                FieldCondition(key="extraction_sha256", match=MatchValue(value=binding.extraction_sha256)),
                FieldCondition(key="rules_edition", match=MatchValue(value=binding.rules_edition)),
                FieldCondition(key="index_version", match=MatchValue(value=index_version)),
            ]
            if active_filter is not None:
                conditions.append(active_filter)
            query_filter = Filter(must=conditions)
            offset = None
            while True:
                points, offset = qdrant.scroll(
                    collection_name=binding.collection,
                    scroll_filter=query_filter,
                    limit=256,
                    offset=offset,
                    with_payload=[
                        "text", "source", "source_sha256", "extraction_sha256", "rules_edition",
                        "index_version", "page", "page_end", "source_pages", "section",
                        "heading_path", "chunk_index", "rule_policy",
                    ],
                    with_vectors=False,
                )
                for point in points:
                    payload = point.payload or {}
                    text = payload.get("text")
                    page = payload.get("page")
                    page_end = payload.get("page_end", page)
                    section = payload.get("section") or payload.get("heading_path")
                    if not isinstance(text, str) or not text.strip():
                        uncitable["book_evidence_incomplete"] += 1
                        continue
                    if (
                        type(page) is not int
                        or page < 1
                        or type(page_end) is not int
                        or page_end < page
                        or not isinstance(section, str)
                        or not section.strip()
                    ):
                        uncitable["book_citation_incomplete"] += 1
                        continue
                    exact_section = payload.get("section") or payload.get("heading_path") or ""
                    policy = rule_policy({"text": text, "section": exact_section, **payload})
                    text_rows.append(
                        {
                            "page": page,
                            "page_end": page_end,
                            "section": section,
                            "text": text,
                            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                            "rule_policy": policy if policy in ("core", "optional", "variant") else "unclassified",
                            "chunk_index": payload.get("chunk_index", 0),
                        }
                    )
                if offset is None:
                    break
        expected_count = receipt.get("chunk_count")
        read_count = len(text_rows) + sum(uncitable.values())
        if type(expected_count) is not int or expected_count < 1 or read_count != expected_count:
            raise GroundingUnavailable(
                "book_evidence_incomplete", f"The active {role.upper()} text does not cover its complete indexed source."
            )
        if uncitable:
            allowed = max(1, int(UNCITABLE_RECORD_BUDGET * expected_count))
            if sum(uncitable.values()) > allowed:
                # A public, recoverable code: the book's index version is not usable as evidence.
                raise GroundingUnavailable(
                    "book_version_unavailable",
                    f"The {role.upper()} has {sum(uncitable.values())} active records without exact "
                    f"text, page or heading; at most {allowed} may be left out.",
                )
            log.warning(
                "book_records_uncitable",
                role=role,
                collection=binding.collection,
                skipped=dict(uncitable),
                allowed=allowed,
                records=expected_count,
            )
        if not text_rows:
            raise GroundingUnavailable("required_book_unavailable", f"No exact text is available for the {role.upper()}.")
        text_rows.sort(key=lambda row: (row["page"], row["page_end"], row["section"], row["text_sha256"]))
        chunks = [
            BookTextEvidence(
                page=row["page"],
                page_end=row["page_end"],
                section=row["section"],
                text=row["text"],
                text_sha256=row["text_sha256"],
                rule_policy=row["rule_policy"],
            )
            for row in text_rows
        ]
        full_text_digest = hashlib.sha256("\n\n".join(row.text for row in chunks).encode("utf-8")).hexdigest()
        books.append(
            VerifiedBookEvidence(
                binding=binding,
                index_version=index_version,
                chunks=chunks,
                text_sha256=full_text_digest,
            )
        )
    try:
        return VerifiedBookSnapshot(rules_edition=rules_edition, books=books)
    except ValueError as exc:
        raise GroundingUnavailable("book_snapshot_invalid", str(exc)) from exc


def study_book_references(
    snapshot: VerifiedBookSnapshot,
    requests: Sequence[BookReferenceRequest],
) -> BookStudyReceipt:
    """P02 helper: exact named study references; a missing item is a blocking receipt."""
    return select_book_evidence(snapshot, requests)


def _supported_mechanic_ids() -> set[str]:
    """Return only exact mechanics with an implementation and regression test in the engine."""
    return {mechanic.id for mechanic in SUPPORTED_MECHANICS}


def _mechanic_matches_party(mechanic_id: str, party: HeroPartySnapshot) -> bool:
    """Check class- and level-gated features against the actual commissioned characters."""
    classes = {hero.character_class.strip().casefold() for hero in party.heroes}
    if mechanic_id == "second_wind":
        return "fighter" in classes
    if mechanic_id == "class_feature_action_surge":
        return any(
            hero.character_class.strip().casefold() == "fighter" and hero.level >= 2
            for hero in party.heroes
        )
    if mechanic_id == "class_feature_sneak_attack":
        return "rogue" in classes
    if mechanic_id == "class_feature_rage":
        return "barbarian" in classes
    return True


def _grounded_creature(name: str, snapshot: VerifiedBookSnapshot, client: Any) -> GroundedCreature:
    mm_evidence = snapshot.for_role("mm")
    mm = mm_evidence.binding
    try:
        record = resolve_creature(name, client=client, rules_edition=snapshot.rules_edition)
    except (CreatureResolutionError, ManifestError) as exc:
        raise GroundingUnavailable("creature_unresolved", "Required exact creature evidence is unavailable or ambiguous.") from exc
    if (
        record.get("collection") != mm.collection
        or record.get("source") != mm.source
        or record.get("source_sha256") != mm.source_sha256
        or record.get("extraction_sha256") != mm.extraction_sha256
        or record.get("rules_edition") != mm.rules_edition
        or record.get("index_version") != mm_evidence.index_version
    ):
        raise GroundingUnavailable(
            "creature_book_mismatch",
            "The creature is not from the bound verified MM source and active index version.",
        )
    text = record.get("text")
    page = record.get("page")
    page_end = record.get("page_end", page)
    if (
        not isinstance(text, str)
        or not text
        or type(page) is not int
        or page < 1
        or type(page_end) is not int
        or page_end < page
    ):
        raise GroundingUnavailable("creature_record_incomplete", "The exact creature record is empty.")
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if record.get("text_sha256") != text_sha256:
        raise GroundingUnavailable("creature_record_incomplete", "The exact creature text digest is invalid.")
    try:
        rating, xp = challenge_xp_from_stat_block(text)
        parsed = parse_monster_stats(text, name=record["creature_name"])
    except (ValueError, MonsterStatsError, KeyError) as exc:
        raise GroundingUnavailable("creature_mechanics_unreadable", "The exact creature record has unreadable required mechanics.") from exc
    return GroundedCreature(
        creature_id=record["creature_id"],
        creature_name=record["creature_name"],
        collection=record["collection"],
        source=record["source"],
        source_sha256=record["source_sha256"],
        extraction_sha256=record["extraction_sha256"],
        rules_edition=record["rules_edition"],
        index_version=record["index_version"],
        page=page,
        page_end=page_end,
        text_sha256=text_sha256,
        stat_block_text=text,
        challenge_rating=rating,
        xp=xp,
        monster_stats=parsed.to_dict(),
    )


def _difficulty_limit(difficulty: str) -> int:
    return ("trivial", "easy", "medium", "hard", "deadly").index(difficulty)


def ground_encounter_plan(
    plan: EncounterPlan,
    party: HeroPartySnapshot,
    books: VerifiedBookSnapshot,
    *,
    client: Any = None,
) -> EncounterGroundingReceipt:
    """P05 helper: resolve exact source records, adapt solo roster, and calculate XP challenge.

    Every record, edition, hero, statistic, and XP number is re-read or computed by code. A
    missing required creature or unsupported claimed mechanic raises a safe error; it never
    returns a ready receipt.
    """
    if books.rules_edition != "2014":
        # The current play engine is explicitly based on 2014 rules even if a source is newer.
        raise GroundingUnavailable("engine_edition_unsupported", "The runtime engine executes the 2014 rules subset only.")
    qdrant = client if client is not None else get_qdrant_client()
    grouped: list[tuple[EncounterCreatureRequest, GroundedCreature]] = []
    for request in plan.creatures:
        grounded = _grounded_creature(request.creature_name, books, qdrant)
        grouped.append((request, grounded))

    requirements_supported, requirements_unsupported = validate_mechanic_requirements(
        plan.mechanics, _supported_mechanic_ids()
    )
    party_unsupported = [
        mechanic
        for mechanic in requirements_supported
        if not _mechanic_matches_party(mechanic.mechanic_id, party)
    ]
    if party_unsupported:
        requirements_supported = [
            mechanic for mechanic in requirements_supported if mechanic not in party_unsupported
        ]
        requirements_unsupported = sorted(
            [*requirements_unsupported, *party_unsupported],
            key=lambda mechanic: (mechanic.category, mechanic.mechanic_id, mechanic.description),
        )
    members = [
        GroundedEncounterMember(
            creature=creature,
            requested_quantity=request.quantity,
            effective_quantity=request.quantity,
            role=request.role,
        )
        for request, creature in grouped
    ]
    adaptation_notes: list[str] = []
    solo_summary = None
    if len(party.heroes) == 1:
        roster: list[dict[str, Any]] = []
        for request, creature in grouped:
            stats = creature.monster_stats
            attacks = stats.get("attacks") or []
            first_attack = attacks[0] if attacks else {}
            for index in range(request.quantity):
                roster.append(
                    {
                        "name": creature.creature_name,
                        "creature_id": creature.creature_id,
                        "hp_max": stats["hp_max"],
                        "hp_current": stats["hp_max"],
                        "attack_bonus": first_attack.get("bonus", 0),
                        "damage_dice": first_attack.get("damage_dice", "0"),
                        "is_boss": request.role == "boss",
                        "_instance": index,
                    }
                )
        fitted, solo_summary = apply_solo_budget(roster, party.heroes[0].level)
        kept = Counter(row["creature_id"] for row in fitted)
        members = [
            member.model_copy(update={"effective_quantity": kept[member.creature.creature_id]})
            for member in members
        ]
        adaptation_notes = list(solo_summary.get("notes") or [])
        if sum(member.effective_quantity for member in members) < 1:
            raise GroundingUnavailable("solo_encounter_empty", "Solo adaptation removed every creature from the encounter.")

    active_members = [member for member in members if member.effective_quantity]
    monster_count = sum(member.effective_quantity for member in active_members)
    raw_xp = sum(member.creature.xp * member.effective_quantity for member in active_members)
    multiplier = encounter_multiplier(monster_count, len(party.heroes))
    adjusted_xp = int(raw_xp * multiplier)
    thresholds = encounter_thresholds(party)
    difficulty = encounter_difficulty(adjusted_xp, thresholds)
    findings = []
    if any(member.creature.monster_stats.get("declared") for member in members):
        findings.append("unsupported_creature_mechanics")
    if difficulty_exceeds(difficulty, plan.maximum_difficulty):
        findings.append("encounter_exceeds_authored_maximum_difficulty")
    if any(hero.level != plan.target_level for hero in party.heroes):
        findings.append("encounter_target_level_mismatch")
    if any(hero.level > 20 or hero.level < 1 for hero in party.heroes):
        findings.append("hero_level_outside_engine_progression")
    if requirements_unsupported:
        findings.append("unsupported_required_mechanics")
    return EncounterGroundingReceipt(
        encounter_id=plan.encounter_id,
        scene_id=plan.scene_id,
        plan_sha256=plan.plan_sha256,
        book_snapshot_sha256=books.snapshot_sha256,
        hero_party_sha256=party.party_sha256,
        rules_edition=books.rules_edition,
        creatures=members,
        raw_xp=raw_xp,
        adjusted_xp=adjusted_xp,
        difficulty=difficulty,
        thresholds=thresholds,
        monster_count_multiplier=multiplier,
        solo_budget=solo_summary,
        adaptation_notes=adaptation_notes,
        supported_mechanics=requirements_supported,
        unsupported_mechanics=requirements_unsupported,
        findings=findings,
    )


def ground_prepared_encounters(
    plans: Sequence[EncounterPlan],
    party: HeroPartySnapshot,
    books: VerifiedBookSnapshot,
    *,
    client: Any = None,
) -> list[EncounterGroundingReceipt]:
    """Ground all encounters for an act; any required failure prevents a complete result."""
    if not plans:
        raise GroundingUnavailable("encounters_missing", "A grounded act must declare its required encounters.")
    ids = [plan.encounter_id for plan in plans]
    if len(ids) != len(set(ids)):
        raise GroundingUnavailable("duplicate_encounter", "Encounter IDs must be unique within the grounded act.")
    return [ground_encounter_plan(plan, party, books, client=client) for plan in plans]


__all__ = [
    "GroundingUnavailable", "ground_prepared_encounters", "ground_encounter_plan",
    "read_verified_book_snapshot", "snapshot_heroes", "study_book_references",
]
