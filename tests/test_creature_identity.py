"""Synthetic exact identities in disposable Qdrant; no embedding/model/network calls."""

import json
from contextlib import closing

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from db.vector.collections import invalidate_collection_cache
from db.vector.creatures import (
    CreatureResolutionError,
    creature_payload_fields,
    resolve_creature,
)
from db.vector.references import normalize_reference
from db.vector.retrieval import RetrievalError, retrieve_monster
from db.vector.versions import begin_index, promote_index
from edgar_core.artifacts import text_digest
from edgar_core.manifests import (
    AdventureManifest,
    StatReference,
    manifest_digest,
    require_published,
)


def stat(name, *, edition="2014", source="synthetic_mm", hp=7, **extra):
    text = (
        f"{name}\nSmall synthetic creature, unaligned\n\n"
        f"Armor Class 15\nHit Points {hp}\nSpeed 30 ft.\n\n"
        + "A synthetic trait with enough text to exceed the old encounter prefix. " * 12
        + "\n\nACTIONS\nSynthetic strike. A synthetic complete final action."
    )
    record = {
        "text": text,
        "section": name,
        "heading_path": name,
        "stat_block": True,
        "source": source,
        "page": 167,
        "page_end": 168,
        "source_pages": [167, 168],
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
        "rules_edition": edition,
        **extra,
    }
    record.update(creature_payload_fields(record))
    return record


@pytest.fixture
def index(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(tmp_path / "locks"))
    # Any accidental old embedding path fails before any provider could be called.
    monkeypatch.setattr(
        "db.vector.retrieval.embed_query", lambda *_: pytest.fail("exact lookup embedded")
    )
    invalidate_collection_cache()
    with closing(QdrantClient(":memory:")) as client:
        yield client
    invalidate_collection_cache()


def put(client, records, collection="rules_monster_manual"):
    if not client.collection_exists(collection):
        client.create_collection(
            collection, vectors_config=VectorParams(size=3, distance=Distance.COSINE)
        )
    client.upsert(
        collection,
        [
            PointStruct(id=i + 1, vector=[1.0, 0.0, 0.0], payload=record)
            for i, record in enumerate(records)
        ],
    )


def test_goblin_never_hobgoblin_or_prose_and_unknown_is_unresolved(index):
    records = [stat("Hobgoblin", hp=97), stat("Goblin")]
    records += [
        {**stat("Goblin"), "stat_block": False, "text": "Goblin mentioned in Hobgoblin prose"}
    ]
    put(index, records)
    exact = retrieve_monster("GOBLIN", client=index, rules_edition="2014")
    assert len(exact) == 1 and exact[0]["creature_key"] == "goblin"
    assert exact[0]["text"] == records[1]["text"]
    assert exact[0]["source_pages"] == [167, 168]
    assert exact[0]["rules_edition"] == "2014"
    assert retrieve_monster("Captain Vellum", client=index) == []
    with pytest.raises(CreatureResolutionError, match="identity unresolved"):
        resolve_creature("Captain Vellum", client=index)


@pytest.mark.parametrize(
    "name", ["Adult Red Dragon", "Ancient Red Dragon", "Young Red Dragon", "Adult Blue Dragon"]
)
def test_all_dragon_qualifiers_are_exact(index, name):
    names = ["Adult Red Dragon", "Ancient Red Dragon", "Young Red Dragon", "Adult Blue Dragon"]
    put(index, [stat(n) for n in names])
    record = resolve_creature(name.lower(), client=index)
    assert record["creature_key"] == normalize_reference(name)
    assert record["creature_name"] == name
    assert retrieve_monster("Red Dragon", client=index) == []


def test_edition_and_source_are_part_of_identity_and_ambiguity_fails(index):
    put(index, [stat("Goblin"), stat("Goblin", edition="2024", hp=8)])
    first = resolve_creature("Goblin", client=index, rules_edition="2014")
    second = resolve_creature("Goblin", client=index, rules_edition="2024")
    assert first["creature_id"] != second["creature_id"]
    assert second["source_sha256"] == "a" * 64
    put(index, [stat("Goblin"), stat("Goblin", source="other_mm", hp=20)])
    with pytest.raises(CreatureResolutionError, match="unique"):
        resolve_creature("Goblin", client=index)


@pytest.mark.parametrize(
    "change",
    [
        {"text": "Hobgoblin\nArmor Class 20\nHit Points 97"},
        {"text": "Goblin\nArmor Class 15\nHit Points 7\nHobgoblin\nArmor Class 20\nHit Points 97"},
        {"stat_block": False},
        {"creature_key": "hobgoblin"},
        {"section": "Actions"},
        {"text": "Goblin\nArmor Class 15\nIncomplete record"},
        {"text": "Goblin\nHobgoblin\nSmall synthetic creature\nArmor Class 20\nHit Points 97"},
    ],
)
def test_mixed_partial_and_conflicting_legacy_records_fail_closed(index, change):
    put(index, [{**stat("Goblin"), **change}])
    assert retrieve_monster("Goblin", client=index) == []


def test_verified_legacy_section_exact_support_and_no_plural_guess(index):
    record = stat("Goblin")
    record.pop("creature_key")
    record.pop("creature_name")
    put(index, [record])
    assert retrieve_monster("goblin", client=index)[0]["creature_name"] == "Goblin"
    assert retrieve_monster("Goblins", client=index) == []


def test_new_encounters_require_explicit_edition_and_verified_provenance(index):
    record = stat("Goblin")
    record.pop("source_sha256")
    put(index, [record])
    with pytest.raises(CreatureResolutionError, match="provenance"):
        resolve_creature("Goblin", client=index)
    put(index, [stat("Goblin", edition="unspecified")])
    with pytest.raises(CreatureResolutionError, match="unique"):
        resolve_creature("Goblin", client=index)


def test_exact_records_beyond_semantic_top_k_and_paginated_duplicates(index):
    # All these exact metadata candidates must be validated, including beyond scroll page 1.
    records = [
        {**stat("Goblin"), "text": "Hobgoblin\nArmor Class 20\nHit Points 97"} for _ in range(260)
    ]
    records.append(stat("Goblin"))
    put(index, records)
    assert resolve_creature("Goblin", client=index)["text"] == records[-1]["text"]


def test_staged_and_obsolete_versions_are_never_resolved(index):
    collection = "rules_monster_manual"
    put(
        index,
        [
            stat("Goblin", index_version="old", source_version="source_old"),
            stat("Goblin", index_version="active", source_version="source_active", hp=9),
            stat("Goblin", index_version="staged", source_version="source_staged", hp=10),
        ],
    )
    token = begin_index(index, collection, "synthetic_mm", "active")
    promote_index(index, collection, "synthetic_mm", "active", token, {}, validate=lambda: None)
    begin_index(index, collection, "synthetic_mm", "staged")
    record = resolve_creature("Goblin", client=index)
    assert record["index_version"] == "active"
    assert record["source_version"] == "source_active"
    assert "Hit Points 9" in record["text"]


def actor_manifest(reference=None, *, module_stat=False):
    public = "A synthetic captain waits in the hall."
    private = (
        stat("Captain Ash")["text"] if module_stat else "The captain privately guards a secret."
    )
    passages = [
        {
            "passage_id": "public",
            "page": 1,
            "start": 0,
            "end": len(public),
            "text_sha256": text_digest(public),
            "scene_ids": ["hall"],
            "entity_ids": ["captain"],
            "audience": "player",
        },
        {
            "passage_id": "stats",
            "page": 2,
            "start": 0,
            "end": len(private),
            "text_sha256": text_digest(private),
            "scene_ids": ["hall"],
            "entity_ids": ["captain"],
            "audience": "planner",
        },
    ]
    entity = {"entity_id": "captain", "kind": "actor", "title": "Captain Ash"}
    if reference:
        entity["stat_reference"] = reference
    manifest = AdventureManifest(
        source="synthetic_module",
        source_sha256="c" * 64,
        extraction_sha256="d" * 64,
        rules_edition="2014",
        opening_scene_id="hall",
        scenes=[{"scene_id": "hall", "title": "Hall"}, {"scene_id": "outside", "title": "Outside"}],
        entities=[entity],
        passages=passages,
        reviewed_by="Synthetic reviewer",
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    chunks = []
    for passage, text in zip(manifest.passages, [public, private], strict=True):
        chunks.append(
            {
                "text": text,
                "source": manifest.source,
                "source_sha256": manifest.source_sha256,
                "extraction_sha256": manifest.extraction_sha256,
                "rules_edition": "2014",
                "manifest_sha256": manifest.reviewed_sha256,
                "passage_id": passage.passage_id,
                "page": passage.page,
                "page_end": passage.page,
                "source_pages": [passage.page],
                "text_sha256": passage.text_sha256,
                "audience": passage.audience,
                "scene_ids": passage.scene_ids,
                "entity_ids": passage.entity_ids,
                "requires": passage.requires,
                "span_start": passage.start,
                "span_end": passage.end,
            }
        )
    return manifest, chunks


def test_named_actor_requires_reviewed_explicit_template(index):
    put(index, [stat("Bandit")])
    reference = {
        "kind": "template",
        "creature_name": "Bandit",
        "collection": "rules_monster_manual",
        "source_sha256": "a" * 64,
        "rules_edition": "2014",
    }
    manifest, _ = actor_manifest(reference)
    resolved = resolve_creature(
        "Captain Ash",
        client=index,
        adventure_manifests={"module": manifest.model_dump()},
        current_scene_id="hall",
    )
    assert resolved["actor_id"] == "captain" and resolved["creature_name"] == "Bandit"
    assert resolved["actor_manifest_sha256"] == manifest.reviewed_sha256
    manifest, _ = actor_manifest()
    with pytest.raises(CreatureResolutionError, match="explicit statistics reference"):
        resolve_creature(
            "Captain Ash",
            client=index,
            adventure_manifests={"module": manifest.model_dump()},
            current_scene_id="hall",
        )


def test_module_record_uses_reviewed_exact_private_span_and_scene_gating(index):
    manifest, chunks = actor_manifest(
        {"kind": "module", "creature_name": "Captain Ash", "passage_id": "stats"}, module_stat=True
    )
    put(index, chunks, collection="module")
    kwargs = {"client": index, "adventure_manifests": {"module": manifest.model_dump()}}
    resolved = resolve_creature("Captain Ash", current_scene_id="hall", **kwargs)
    assert resolved["text"] == chunks[1]["text"] and resolved["kind"] == "adventure"
    assert resolved["audience"] == "planner" and resolved["source_sha256"] == "c" * 64
    with pytest.raises(CreatureResolutionError, match="outside"):
        resolve_creature("Captain Ash", current_scene_id="outside", **kwargs)
    # A mutation in the indexed reviewed statistics fails source validation, not fallback.
    chunks[1]["text"] = stat("Hobgoblin")["text"]
    put(index, chunks, collection="module")
    with pytest.raises(ValueError, match="differs"):
        resolve_creature("Captain Ash", current_scene_id="hall", **kwargs)


def test_missing_template_source_or_edition_never_borrows_another(index):
    put(index, [stat("Bandit", edition="2024")])
    manifest, _ = actor_manifest(
        {
            "kind": "template",
            "creature_name": "Bandit",
            "collection": "rules_monster_manual",
            "source_sha256": "a" * 64,
            "rules_edition": "2014",
        }
    )
    with pytest.raises(CreatureResolutionError):
        resolve_creature(
            "Captain Ash",
            client=index,
            adventure_manifests={"module": manifest.model_dump()},
            current_scene_id="hall",
        )


def test_template_wrong_source_checksum_and_future_actor_cannot_use_generic_record(index):
    put(index, [stat("Bandit")])
    reference = {
        "kind": "template",
        "creature_name": "Bandit",
        "collection": "rules_monster_manual",
        "source_sha256": "f" * 64,
        "rules_edition": "2014",
    }
    manifest, _ = actor_manifest(reference)
    with pytest.raises(CreatureResolutionError, match="unique"):
        resolve_creature(
            "Captain Ash",
            client=index,
            adventure_manifests={"module": manifest.model_dump()},
            current_scene_id="hall",
        )
    # Even an exact generic record cannot bypass the reviewed actor's scene restriction.
    put(index, [stat("Captain Ash")])
    manifest, _ = actor_manifest()
    with pytest.raises(CreatureResolutionError, match="outside"):
        resolve_creature(
            "Captain Ash",
            client=index,
            adventure_manifests={"module": manifest.model_dump()},
            current_scene_id="outside",
        )


def test_locked_module_statistics_cannot_be_used_before_prerequisites(index):
    manifest, _ = actor_manifest(
        {"kind": "module", "creature_name": "Captain Ash", "passage_id": "stats"}, module_stat=True
    )
    manifest.passages[1].requires = {"captain_revealed": "true"}
    manifest.reviewed_sha256 = manifest_digest(manifest)
    with pytest.raises(CreatureResolutionError, match="locked"):
        resolve_creature(
            "Captain Ash",
            client=index,
            adventure_manifests={"module": manifest.model_dump()},
            current_scene_id="hall",
        )


def test_manifest_binding_receipt_and_old_digest_compatibility():
    manifest, _ = actor_manifest()
    old_body = manifest.model_dump(exclude={"reviewed_sha256", "reviewed_by"})
    for entity in old_body["entities"]:
        entity.pop("stat_reference")
    old_digest = text_digest(
        json.dumps(old_body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )
    assert manifest_digest(manifest) == old_digest
    assert require_published(manifest).reviewed_sha256 == old_digest
    manifest.entities[0].stat_reference = StatReference(
        **{
            "kind": "module",
            "creature_name": "Captain Ash",
            "passage_id": "stats",
        }
    )
    with pytest.raises(ValueError):
        require_published(manifest)


def test_transport_failure_is_not_an_empty_resolved_result(index, monkeypatch):
    put(index, [stat("Goblin")])
    monkeypatch.setattr(
        index, "scroll", lambda **kwargs: (_ for _ in ()).throw(ConnectionError("unavailable"))
    )
    with pytest.raises(RetrievalError, match="Exact creature retrieval failed"):
        retrieve_monster("Goblin", client=index)
