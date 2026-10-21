"""Focused source continuity checks; no database or provider is needed."""

from copy import deepcopy

import pytest

from apps.api.services.campaign_prep.compile import assemble, make_extraction
from apps.api.services.campaign_prep.store import NeedsAttention
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest, select_scene_context
from ingestion.manifest import manifest_chunks
from tests.test_campaign_graph_v2 import sample


def source_fixture():
    return {
        "kind": "pdf",
        "sha256": "a" * 64,
        "directory": "/data/campaign/source",
        "pages": [
            {
                "page": number,
                "text": text,
                "image": f"page-{number:04d}.png",
                "image_sha256": "b" * 64,
                "width": 1000,
                "height": 1400,
                "regions": [{"id": f"p{number}-b1", "bbox": [1, 2, 80, 90]}],
                "quality": {"mean_confidence": 93},
            }
            for number, text in (
                (1, "The keeper requests aid. The rope bridge is forty feet above the river."),
                (
                    2,
                    "Appendix: The silver key has three charges. A charge returns at dawn.\nDC 15 Arcana identifies it.",
                ),
            )
        ],
    }


def test_full_source_is_private_retrievable_and_shared_without_copying():
    spine, bundles = sample()
    spine["chapters"][0]["pages"] = [1, 2]
    next_chapter = deepcopy(spine["chapters"][0])
    next_chapter.update(id="ch_after", title="Afterward", pages=[2], play_order=2)
    spine["chapters"].append(next_chapter)
    next_bundle = deepcopy(bundles["chapters"][0])
    next_bundle["chapter_id"] = "ch_after"
    next_bundle["goals"][0].update(id="goal_after", event_ids=["event_after"])
    next_bundle["events"][0].update(id="event_after", goal_ids=["goal_after"])
    bundles["chapters"].append(next_bundle)
    source = source_fixture()
    before = deepcopy(source)
    compiled = assemble("fresh", "2014", spine, bundles, [], source=source)
    assert source == before
    manifest = AdventureManifest.model_validate(compiled["manifest"])
    manifest.reviewed_by = "focused-source-retention-test"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    chunks = manifest_chunks(make_extraction(compiled), manifest)
    source_chunks = [c for c in chunks if c["passage_id"].startswith("source_page_")]
    assert len(source_chunks) == 2
    assert all(c["audience"] == "planner" for c in source_chunks)
    assert source["pages"][1]["text"] in source_chunks[1]["text"]
    assert "page-0002.png" in source_chunks[1]["text"]
    assert "mean_confidence" in source_chunks[1]["text"]
    assert compiled["source_references"][1]["ocr_text_sha256"] == text_digest(
        source["pages"][1]["text"]
    )
    first = select_scene_context(manifest, "event_square")
    second = select_scene_context(manifest, "event_after")
    assert "source_page_0001" in first["planner_passage_ids"]
    assert "source_page_0001" not in second["planner_passage_ids"]
    assert "source_page_0002" in second["planner_passage_ids"]
    assert "source_page_0002" not in first["player_passage_ids"]
    assert all("charges" not in c["text"] for c in chunks if c["audience"] == "player")


def test_unassigned_source_page_cannot_disappear_at_compilation():
    spine, bundles = sample()
    spine["chapters"][0]["pages"] = [1]
    with pytest.raises(NeedsAttention, match="absent from the chapter plan"):
        assemble("fresh", "2014", spine, bundles, [], source=source_fixture())


def test_source_items_checks_and_profiles_reach_real_engine_records():
    spine, bundles = sample()
    spine["chapters"][0]["pages"] = [1, 2]
    bundle = bundles["chapters"][0]
    bundle["source_pages"] = [1, 2]
    bundle["source_notes"] = (
        "The bridge is forty feet above the river; the key restores one charge at dawn."
    )
    origin = {"kind": "pdf", "pages": [2], "regions": [], "note": "", "uncertain": False}
    for identity, kind, name in (
        ("silver_key", "clue", "Silver key"),
        ("guard", "npc", "Bridge Guard"),
    ):
        bundle["new_entities"].append(
            {
                "id": identity,
                "kind": kind,
                "name": name,
                "description": name,
                "origin": origin,
            }
        )
    bundle["items"] = [
        {
            "entity_id": "silver_key",
            "procedure": source_fixture()["pages"][1]["text"],
            "event_ids": ["event_square"],
            "portable": True,
            "consumable": False,
            "origin": origin,
        }
    ]
    bundle["checks"] = [
        {
            "id": "inspect_key",
            "action": "Inspect the key's magical properties",
            "event_ids": ["event_square"],
            "check": "arcana",
            "dc": 15,
            "success_text": "Recognize the key's unreliability.",
            "failure_text": "Its construction remains unclear.",
            "origin": origin,
        }
    ]
    event = bundle["events"][0]
    event["participants"] = ["guard"]
    event["clue_ids"] = ["silver_key"]
    event["encounters"] = [
        {
            "actor_id": "guard",
            "creature": "Bridge Guard",
            "quantity": 1,
            "adaptation": "",
            "source_pages": [2],
            "source_stat_block": """Bridge Guard
Medium humanoid, neutral
Armor Class 14
Hit Points 21 (3d8 + 6)
Speed 30 ft.
STR DEX CON INT WIS CHA
14 (+2) 12 (+1) 14 (+2) 10 (+0) 10 (+0) 10 (+0)
Challenge 1 (200 XP)
Actions
Sword. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 6 (1d8 + 2) slashing damage.
""",
        }
    ]
    compiled = assemble("fresh", "2014", spine, bundles, [], source=source_fixture())
    manifest = AdventureManifest.model_validate(compiled["manifest"])
    obj = manifest.objects[0]
    assert obj.canonical_id == "silver_key_object" and not obj.operations
    key = next(e for e in manifest.entities if e.entity_id == "silver_key")
    assert key.object_id == obj.canonical_id
    guard = next(e for e in manifest.entities if e.entity_id == "guard")
    assert guard.stat_reference.kind == "module"
    assert guard.stat_reference.creature_name == "Bridge Guard"
    assert manifest.resolution_policies[0].dc == 15
    assert manifest.resolution_policies[0].check == "arcana"
    assert not manifest.resolution_policies[0].on_success
    assert not manifest.resolution_policies[0].on_failure
    text = "\n".join(page["text"] for page in compiled["pages"])
    assert bundle["items"][0]["procedure"] in text
    assert "forty feet" in compiled["guide"]
    assert compiled["source_profiles"]["guard"]["monster_stats"]["hp_max"] == 21
    assert any(
        "no executable use" in finding["detail"] for finding in compiled["capability_findings"]
    )
    assert any(
        finding.get("check_id") == "inspect_key"
        and "roll only" in finding["detail"]
        and "no associated discovery" in finding["detail"]
        for finding in compiled["capability_findings"]
    )
    assert "A successful roll does not itself commit a discovery" in text
