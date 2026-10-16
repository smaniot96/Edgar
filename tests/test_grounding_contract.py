"""Pure, offline contracts for book/hero snapshots and engine-owned encounter budgets."""

import hashlib
from copy import deepcopy

import pytest
from pydantic import ValidationError

from edgar_core.grounding import (
    BookBinding,
    BookReferenceRequest,
    BookTextEvidence,
    EncounterThresholds,
    HeroPartySnapshot,
    HeroSnapshot,
    MechanicRequirement,
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


def _book(role: str, collection: str, section: str, text: str, *, policy="core"):
    hash_char = {"phb": "1", "dmg": "2", "mm": "3"}[role]
    binding = BookBinding(
        role=role,
        collection=collection,
        source=f"{role}.pdf",
        source_sha256=(hash_char * 64),
        extraction_sha256=(str(int(hash_char) + 3) * 64),
        rules_edition="2014",
    )
    chunk = BookTextEvidence(
        page=1,
        page_end=1,
        section=section,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        rule_policy=policy,
    )
    full_hash = hashlib.sha256(text.encode()).hexdigest()
    return VerifiedBookEvidence(
        binding=binding,
        index_version="f" * 64,
        chunks=[chunk],
        text_sha256=full_hash,
    )


def _books() -> VerifiedBookSnapshot:
    return VerifiedBookSnapshot(
        rules_edition="2014",
        books=[
            _book("phb", "rules_player_handbook", "Character Advancement", "PHB text."),
            _book("dmg", "rules_dm_guide", "Encounter Difficulty", "DMG text."),
            _book("mm", "rules_monster_manual", "Goblin", "Goblin stats."),
        ],
    )


def _party(*levels: int) -> HeroPartySnapshot:
    return HeroPartySnapshot(
        heroes=[
            HeroSnapshot(
                character_id=index,
                name=f"Hero {index}",
                character_class="Fighter",
                level=level,
                hp_max=10 + level,
                stats={"STR": 15, "CON": 14},
                inventory={"items": ["sword"]},
            )
            for index, level in enumerate(levels, start=1)
        ]
    )


def test_verified_book_snapshot_requires_all_exact_distinct_roles_and_one_edition():
    books = _books()
    assert len(books.snapshot_sha256) == 64
    assert books.for_role("mm").binding.extraction_sha256 == "6" * 64
    with pytest.raises(ValidationError, match="at least 3 items"):
        VerifiedBookSnapshot(rules_edition="2014", books=books.books[:2])
    changed = deepcopy([book.model_dump() for book in books.books])
    changed[2]["binding"]["rules_edition"] = "2024"
    with pytest.raises(ValidationError, match="match the snapshot edition"):
        VerifiedBookSnapshot(rules_edition="2014", books=changed)


def test_exact_excerpt_changes_snapshot_digest_and_text_hash_is_checked():
    books = _books()
    before = books.snapshot_sha256
    chunks = books.for_role("phb").chunks[0].model_copy(
        update={"text": "Corrected source excerpt.", "text_sha256": hashlib.sha256(b"Corrected source excerpt.").hexdigest()}
    )
    changed_book = books.for_role("phb").model_copy(update={
        "chunks": [chunks],
        "text_sha256": hashlib.sha256(b"Corrected source excerpt.").hexdigest(),
    })
    changed = VerifiedBookSnapshot(
        rules_edition="2014",
        books=[changed_book if row.binding.role == "phb" else row for row in books.books],
    )
    assert changed.snapshot_sha256 != before
    with pytest.raises(ValidationError, match="text digest"):
        BookTextEvidence(
            page=1, page_end=1, section="x", text="x", text_sha256="0" * 64
        )


def test_book_study_selects_only_exact_core_heading_and_blocks_missing_or_optional():
    books = _books()
    dmg = books.for_role("dmg")
    optional = dmg.model_copy(update={"chunks": [
        dmg.chunks[0].model_copy(update={"section": "Gritty Realism", "rule_policy": "optional"})
    ]})
    snapshot = VerifiedBookSnapshot(
        rules_edition="2014",
        books=[optional if row.binding.role == "dmg" else row for row in books.books],
    )
    receipt = select_book_evidence(
        snapshot,
        [
            BookReferenceRequest(role="phb", references=["Character Advancement"]),
            BookReferenceRequest(role="dmg", references=["Gritty Realism", "Resting"]),
        ],
    )
    assert [row.section for row in receipt.evidence] == ["Character Advancement"]
    assert receipt.missing_references == ["dmg:Gritty Realism", "dmg:Resting"]
    assert not receipt.valid


def test_actual_party_digest_binds_inventory_class_and_level():
    party = _party(1, 8)
    assert party.party_sha256 == HeroPartySnapshot(heroes=list(reversed(party.heroes))).party_sha256
    changed_hero = party.heroes[0].model_copy(update={"inventory": {"items": ["magic key"]}})
    changed = HeroPartySnapshot(heroes=[changed_hero, party.heroes[1]])
    assert changed.party_sha256 != party.party_sha256
    with pytest.raises(ValidationError, match="cannot repeat"):
        HeroPartySnapshot(heroes=[party.heroes[0], party.heroes[0]])


def test_engine_xp_and_difficulty_use_printed_verified_cr_and_real_hero_levels():
    assert challenge_xp_from_stat_block("Goblin\nChallenge 1/4 (50 XP)") == ("1/4", 50)
    with pytest.raises(ValueError, match="conflicts"):
        challenge_xp_from_stat_block("Goblin\nChallenge 1/4 (100 XP)")
    with pytest.raises(ValueError, match="one complete"):
        challenge_xp_from_stat_block("Goblin\nNo challenge line")

    thresholds = encounter_thresholds(_party(1))
    assert thresholds == EncounterThresholds(easy=25, medium=50, hard=75, deadly=100)
    assert encounter_difficulty(99, thresholds) == "hard"
    assert encounter_difficulty(100, thresholds) == "deadly"
    assert encounter_multiplier(1, 1) == 1.5
    assert encounter_multiplier(3, 1) == 2.5
    assert encounter_multiplier(3, 6) == 1.5
    assert difficulty_exceeds("hard", "medium")
    assert not difficulty_exceeds("hard", "deadly")


def test_unimplemented_resource_treasure_and_xp_leveling_claims_are_reported():
    requirements = [
        MechanicRequirement(category="rest", mechanic_id="long_rest"),
        MechanicRequirement(category="treasure", mechanic_id="magic_item_effect"),
        MechanicRequirement(category="progression", mechanic_id="experience_leveling"),
        MechanicRequirement(category="hazard", mechanic_id="hazard_d20_check"),
    ]
    supported, unsupported = validate_mechanic_requirements(
        requirements, {"long_rest", "hazard_d20_check"}
    )
    assert {row.mechanic_id for row in supported} == {"long_rest", "hazard_d20_check"}
    assert {row.mechanic_id for row in unsupported} == {"magic_item_effect", "experience_leveling"}
