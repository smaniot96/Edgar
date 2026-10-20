"""Offline production adapters over synthetic, disposable active Qdrant sources."""

import hashlib
from contextlib import closing

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from apps.api.services import preparation_grounding
from apps.api.services.preparation_grounding import (
    GroundingUnavailable,
    ground_encounter_plan,
    read_verified_book_snapshot,
    snapshot_heroes,
    study_book_references,
)
from db.vector.collections import invalidate_collection_cache
from db.vector.creatures import creature_payload_fields
from db.vector.versions import begin_index, promote_index
from edgar_core.grounding import (
    BookBinding,
    BookReferenceRequest,
    EncounterCreatureRequest,
    EncounterPlan,
    HeroPartySnapshot,
    HeroSnapshot,
    MechanicRequirement,
    challenge_xp_from_stat_block,
)

GOBLIN = """Goblin
Small humanoid (goblinoid), neutral evil
Armor Class 15 (leather armor, shield)
Hit Points 7 (2d6)
Speed 30 ft.
STR DEX CON INT WIS CHA
8 (-1) 14 (+2) 10 (+0) 10 (+0) 8 (-1) 8 (-1)
Senses darkvision 60 ft.
Challenge 1/4 (50 XP)
Actions
Scimitar. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 5 (1d6 + 2) slashing damage.
"""


def _binding(role):
    collection = {
        "phb": "rules_player_handbook",
        "dmg": "rules_dm_guide",
        "mm": "rules_monster_manual",
    }[role]
    number = {"phb": "1", "dmg": "2", "mm": "3"}[role]
    return BookBinding(
        role=role,
        collection=collection,
        source=f"synthetic_{role}.pdf",
        source_sha256=number * 64,
        extraction_sha256=str(int(number) + 3) * 64,
        rules_edition="2014",
    )


def _index(client, binding, text, *, page=10, section="Synthetic rules"):
    collection = binding.collection
    version = {"phb": "a", "dmg": "b", "mm": "c"}[binding.role] * 64
    if not client.collection_exists(collection):
        client.create_collection(
            collection, vectors_config=VectorParams(size=3, distance=Distance.COSINE)
        )
    payload = {
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source": binding.source,
        "source_sha256": binding.source_sha256,
        "extraction_sha256": binding.extraction_sha256,
        "rules_edition": binding.rules_edition,
        "page": page,
        "page_end": page,
        "section": section,
        "chunk_index": 0,
        "index_version": version,
    }
    if binding.role == "mm":
        payload["stat_block"] = True
        payload.update(creature_payload_fields(payload))
    client.upsert(
        collection,
        [PointStruct(id=1, vector=[1.0, 0.0, 0.0], payload=payload)],
    )
    token = begin_index(client, collection, binding.source, version)
    promote_index(
        client,
        collection,
        binding.source,
        version,
        token,
        {
            "source_sha256": binding.source_sha256,
            "extraction_sha256": binding.extraction_sha256,
            "rules_edition": binding.rules_edition,
            "chunk_count": 1,
        },
        validate=lambda: None,
    )
    return version


@pytest.fixture
def source_index(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(tmp_path / "locks"))
    invalidate_collection_cache()
    with closing(QdrantClient(":memory:")) as client:
        bindings = [_binding(role) for role in ("phb", "dmg", "mm")]
        for binding in bindings:
            text = GOBLIN if binding.role == "mm" else f"{binding.role.upper()} Character Advancement excerpt."
            _index(
                client,
                binding,
                text,
                page=42 if binding.role == "phb" else 77 if binding.role == "dmg" else 167,
                section="Goblin" if binding.role == "mm" else "Character Advancement",
            )
        yield client, bindings
    invalidate_collection_cache()


def _party(level=1):
    return HeroPartySnapshot(
        heroes=[
            HeroSnapshot(
                character_id=9,
                name="Actual Hero",
                character_class="Fighter",
                level=level,
                hp_max=max(8, level * 7),
                stats={"STR": 15, "CON": 14},
                inventory={"items": ["longsword", "shield"]},
            )
        ]
    )


def test_actual_book_reader_uses_exact_active_payloads_without_embedding(source_index, monkeypatch):
    client, bindings = source_index
    monkeypatch.setattr(
        "db.vector.retrieval.embed_query", lambda *_: pytest.fail("book opening must not embed")
    )
    books = read_verified_book_snapshot(bindings, rules_edition="2014", client=client)
    assert [row.binding.role for row in books.books] == ["phb", "dmg", "mm"]
    assert books.for_role("phb").chunks[0].page == 42
    assert books.for_role("mm").chunks[0].text_sha256 == hashlib.sha256(GOBLIN.encode()).hexdigest()
    selected = study_book_references(
        books,
        [BookReferenceRequest(role="phb", references=["Character Advancement"])],
    )
    assert selected.valid
    assert selected.evidence[0].page == 42


def test_required_book_edition_or_collection_mismatch_blocks(source_index):
    client, bindings = source_index
    with pytest.raises(GroundingUnavailable, match="match the selected edition"):
        read_verified_book_snapshot(bindings, rules_edition="2024", client=client)
    wrong_role = [bindings[0], bindings[1], bindings[2].model_copy(update={"collection": "rules_player_handbook"})]
    with pytest.raises(GroundingUnavailable, match="wrong source collection"):
        read_verified_book_snapshot(wrong_role, rules_edition="2014", client=client)


def test_actual_hero_source_alias_is_snapshotted_and_changes_digest():
    one = snapshot_heroes(
        [{"id": 2, "name": "Mage", "character_class": "Wizard", "level": 5, "hp_max": 27,
          "stats": {"INT": 18}, "inventory": {"items": ["wand"]}}]
    )
    changed = snapshot_heroes(
        [{"id": 2, "name": "Mage", "character_class": "Wizard", "level": 6, "hp_max": 27,
          "stats": {"INT": 18}, "inventory": {"items": ["wand"]}}]
    )
    assert one.heroes[0].character_id == 2
    assert one.party_sha256 != changed.party_sha256
    with pytest.raises(GroundingUnavailable, match="incomplete or malformed"):
        snapshot_heroes([{"id": 2, "name": "Mage"}])


def test_encounter_uses_exact_mm_stats_actual_hero_and_engine_solo_budget(source_index):
    client, bindings = source_index
    books = read_verified_book_snapshot(bindings, rules_edition="2014", client=client)
    plan = EncounterPlan(
        encounter_id="dock_fight",
        scene_id="ferry_dock",
        target_level=1,
        creatures=[EncounterCreatureRequest(creature_name="Goblin", quantity=3)],
        maximum_difficulty="deadly",
        mechanics=[
            MechanicRequirement(category="action", mechanic_id="weapon_attacks"),
            MechanicRequirement(category="rest", mechanic_id="short_rest"),
            MechanicRequirement(category="treasure", mechanic_id="magic_item_effect"),
            MechanicRequirement(category="progression", mechanic_id="xp_level_advancement"),
        ],
    )
    receipt = ground_encounter_plan(plan, _party(), books, client=client)
    assert receipt.creatures[0].creature.creature_id
    assert receipt.creatures[0].creature.text_sha256 == hashlib.sha256(GOBLIN.encode()).hexdigest()
    assert receipt.creatures[0].creature.page == 167
    assert receipt.creatures[0].creature.index_version == books.for_role("mm").index_version
    assert receipt.creatures[0].creature.monster_stats["hp_max"] == 7
    assert receipt.creatures[0].creature.challenge_rating == "1/4"
    assert receipt.creatures[0].creature.xp == 50
    assert receipt.creatures[0].requested_quantity == 3
    assert receipt.creatures[0].effective_quantity == 2
    assert receipt.solo_budget["level"] == 1
    assert receipt.adaptation_notes
    assert receipt.raw_xp == 100
    assert receipt.adjusted_xp == 200
    assert receipt.difficulty == "deadly"
    assert {row.mechanic_id for row in receipt.unsupported_mechanics} == {
        "magic_item_effect",
        "xp_level_advancement",
    }
    assert not receipt.valid


def test_exact_creature_missing_from_bound_mm_cannot_fall_back_to_model_memory(source_index):
    client, bindings = source_index
    books = read_verified_book_snapshot(bindings, rules_edition="2014", client=client)
    plan = EncounterPlan(
        encounter_id="tower_guard",
        scene_id="tower",
        target_level=20,
        creatures=[EncounterCreatureRequest(creature_name="Adult Red Dragon", quantity=1, role="boss")],
        maximum_difficulty="deadly",
    )
    with pytest.raises(GroundingUnavailable, match="evidence is unavailable or ambiguous") as caught:
        ground_encounter_plan(plan, _party(level=20), books, client=client)
    assert caught.value.code == "creature_unresolved"


def test_creature_active_version_must_match_frozen_mm_snapshot(source_index, monkeypatch):
    client, bindings = source_index
    books = read_verified_book_snapshot(bindings, rules_edition="2014", client=client)
    original = preparation_grounding.resolve_creature

    def stale_record(*args, **kwargs):
        return {**original(*args, **kwargs), "index_version": "d" * 64}

    monkeypatch.setattr(preparation_grounding, "resolve_creature", stale_record)
    plan = EncounterPlan(
        encounter_id="dock_fight",
        scene_id="ferry_dock",
        target_level=1,
        creatures=[EncounterCreatureRequest(creature_name="Goblin", quantity=1)],
        maximum_difficulty="deadly",
    )
    with pytest.raises(GroundingUnavailable, match="active index version") as caught:
        ground_encounter_plan(plan, _party(), books, client=client)
    assert caught.value.code == "creature_book_mismatch"


def test_target_level_and_class_specific_mechanics_are_bound_to_actual_party(source_index):
    client, bindings = source_index
    books = read_verified_book_snapshot(bindings, rules_edition="2014", client=client)
    plan = EncounterPlan(
        encounter_id="training_hall",
        scene_id="hall",
        target_level=2,
        creatures=[EncounterCreatureRequest(creature_name="Goblin", quantity=1)],
        maximum_difficulty="deadly",
        mechanics=[MechanicRequirement(category="resource", mechanic_id="class_feature_action_surge")],
    )
    receipt = ground_encounter_plan(plan, _party(level=1), books, client=client)
    assert "encounter_target_level_mismatch" in receipt.findings
    assert {row.mechanic_id for row in receipt.unsupported_mechanics} == {"class_feature_action_surge"}
    assert not receipt.valid


def test_xp_parser_rejects_mismatched_challenge_value():
    assert challenge_xp_from_stat_block(GOBLIN) == ("1/4", 50)
    with pytest.raises(ValueError, match="conflicts"):
        challenge_xp_from_stat_block(GOBLIN.replace("50 XP", "100 XP"))


def test_unimplemented_monster_trait_blocks_grounding_even_if_not_authored(source_index):
    client, bindings = source_index
    mm = bindings[2]
    _index(client, mm, GOBLIN.replace("Actions\n", "Legendary Resistance (3/Day). If the goblin fails a saving throw, it can choose to succeed instead.\nActions\n"), section="Goblin")
    books = read_verified_book_snapshot(bindings, rules_edition="2014", client=client)
    plan = EncounterPlan(encounter_id="guard", scene_id="hall", target_level=1,
                         creatures=[EncounterCreatureRequest(creature_name="Goblin", quantity=1)],
                         maximum_difficulty="deadly")
    receipt = ground_encounter_plan(plan, _party(), books, client=client)
    assert receipt.creatures[0].creature.monster_stats["declared"]
    assert "unsupported_creature_mechanics" in receipt.findings
    assert not receipt.valid


def _index_rows(client, binding, rows):
    """Index several records of one book; `rows` are (text, page, section) triples."""
    collection = binding.collection
    version = {"phb": "a", "dmg": "b", "mm": "c"}[binding.role] * 64
    if not client.collection_exists(collection):
        client.create_collection(
            collection, vectors_config=VectorParams(size=3, distance=Distance.COSINE)
        )
    points = []
    for index, (text, page, section) in enumerate(rows, 1):
        payload = {
            "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source": binding.source,
            "source_sha256": binding.source_sha256,
            "extraction_sha256": binding.extraction_sha256,
            "rules_edition": binding.rules_edition,
            "page": page,
            "page_end": page,
            "section": section,
            "chunk_index": index,
            "index_version": version,
        }
        points.append(PointStruct(id=index, vector=[1.0, 0.0, 0.0], payload=payload))
    client.upsert(collection, points)
    token = begin_index(client, collection, binding.source, version)
    promote_index(
        client,
        collection,
        binding.source,
        version,
        token,
        {
            "source_sha256": binding.source_sha256,
            "extraction_sha256": binding.extraction_sha256,
            "rules_edition": binding.rules_edition,
            "chunk_count": len(rows),
        },
        validate=lambda: None,
    )


@pytest.fixture
def books_with_an_uncitable_record(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(tmp_path / "locks"))
    invalidate_collection_cache()
    with closing(QdrantClient(":memory:")) as client:
        bindings = [_binding(role) for role in ("phb", "dmg", "mm")]
        for binding in bindings[1:]:
            text = GOBLIN if binding.role == "mm" else "DMG Character Advancement excerpt."
            _index(client, binding, text, section="Goblin" if binding.role == "mm" else "Advancement")
        yield client, bindings
    invalidate_collection_cache()


def test_an_uncitable_ocr_record_is_left_out_not_the_whole_book(books_with_an_uncitable_record):
    client, bindings = books_with_an_uncitable_record
    _index_rows(
        client,
        bindings[0],
        [("PHB Character Advancement excerpt.", 42, "Character Advancement"),
         ("must succeed 00 a Coostitutioo saviog throw", 270, None)],
    )
    books = read_verified_book_snapshot(bindings, rules_edition="2014", client=client)
    assert [c.page for c in books.for_role("phb").chunks] == [42]


def test_a_book_with_too_many_uncitable_records_stays_unavailable(books_with_an_uncitable_record):
    client, bindings = books_with_an_uncitable_record
    _index_rows(client, bindings[0], [("damaged one", 270, None), ("damaged two", 271, "")])
    with pytest.raises(GroundingUnavailable) as caught:
        read_verified_book_snapshot(bindings, rules_edition="2014", client=client)
    assert caught.value.code == "book_version_unavailable"
