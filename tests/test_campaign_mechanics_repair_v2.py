"""Mechanical identity defects use scoped semantic repair without reauthoring bundles."""

from copy import deepcopy

import pytest
from test_campaign_graph_v2 import ORIGIN, sample

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep import publication
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.mechanical_findings import mechanical_findings
from apps.api.services.campaign_prep.store import Store
from edgar_core.campaign_semantics import Bundles, Entity, Patch, Review, Spine


def fixture():
    spine, bundles = deepcopy(sample())
    actor = Entity(
        id="npc_guard", kind="npc", name="Guard", description="A guard", origin=ORIGIN
    ).model_dump()
    spine["entities"].append(actor)
    event = bundles["chapters"][0]["events"][0]
    event["participants"] = ["npc_guard"]
    event["encounters"] = [
        {"actor_id": "npc_guard", "creature": "Orc", "quantity": 1, "adaptation": ""},
        {"actor_id": "npc_guard", "creature": "Wolf", "quantity": 3, "adaptation": ""},
    ]
    return Spine.model_validate(spine).model_dump(), Bundles.model_validate(bundles).model_dump()


def test_incompatible_templates_are_specific_material_findings():
    _, bundles = fixture()
    result = mechanical_findings(bundles, [{"name": "Orc"}, {"name": "Wolf"}])
    assert len(result) == 1
    assert result[0]["record_ids"] == ["event_square", "npc_guard"]
    assert "Wolf" in result[0]["evidence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_existing", [False, True])
async def test_saved_authoring_is_reused_and_new_actor_stays_in_affected_bundle(
    tmp_path, monkeypatch, reuse_existing
):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path)
    store = Store("repair_mechanics")
    store.initialize(
        {"source_kind": "ai", "generation": {}, "preferences": "", "rules_edition": "2014"}
    )
    spine, bundles = fixture()
    existing = Entity(
        id="npc_wolves", kind="npc", name="Guard wolves", description="Original wolves", origin=ORIGIN
    ).model_dump()
    if reuse_existing:
        spine["entities"].append(existing)
    store.put("source", {"kind": "ai", "pages": []})
    store.put("spine", spine)
    store.put("bundles", bundles)
    store.put(
        "mechanics",
        [
            {
                "name": name,
                "collection": "verified_mm",
                "source_sha256": "a" * 64,
                "rules_edition": "2014",
            }
            for name in ["Orc", "Wolf"]
        ],
    )
    calls = []

    class Semantic:
        async def call(self, node, prompt, schema, evidence, *args):
            calls.append(prompt)
            if schema is Patch:
                assert set(evidence["allowed_ids"]) == {"npc_guard", "event_square", "goal_keeper"}
                event = deepcopy(bundles["chapters"][0]["events"][0])
                event["participants"].append("npc_wolves")
                event["encounters"][1]["actor_id"] = "npc_wolves"
                wolves = Entity(
                    id="npc_wolves",
                    kind="npc",
                    name="Guard wolves",
                    description="Three wolves",
                    origin=ORIGIN,
                )
                return Patch(events=[event], entities=[wolves])
            assert schema is Review
            return Review(covered_ids=evidence["review_ids"])

    monkeypatch.setattr(publication, "publish", lambda s: s.put("publication", {"ok": True}))
    await Workflow(store, semantic=Semantic()).run()
    assert store.state()["status"] == "ready"
    assert calls == ["repair", "review"]
    assert store.get("spine") == spine
    repaired = store.get("bundles")["chapters"][0]
    if reuse_existing:
        assert repaired["new_entities"] == []
        assert next(e for e in store.get("spine")["entities"] if e["id"] == "npc_wolves") == existing
    else:
        assert repaired["new_entities"][0]["id"] == "npc_wolves"
    assert repaired["events"][0]["encounters"][1]["quantity"] == 3
    assert mechanical_findings(store.get("bundles"), store.get("mechanics")) == []


@pytest.mark.asyncio
async def test_retained_pdf_collects_images_for_existing_calls_without_visual_stage(
    tmp_path, monkeypatch
):
    from apps.api.services.campaign_prep import graph

    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path)
    store = Store("retained_visual")
    store.initialize({"source_kind": "pdf", "rules_edition": "2014"})
    image = tmp_path / "page-0004.png"
    image.write_bytes(b"original-source-map")
    source = {
        "kind": "pdf",
        "pages": [{"page": 4, "image": image.name}],
        "directory": str(tmp_path),
    }
    store.put("source", source)
    store.put("mechanics", [])
    spine, bundles = sample()
    store.put("spine", spine)
    store.put("bundles", bundles)
    monkeypatch.setattr(graph, "candidate_visual_pages", lambda value: [4])
    monkeypatch.setattr(graph, "visual_page_reasons", lambda *_: ["source map"])
    workflow = Workflow(store, semantic=object())

    await workflow.source({"slug": store.slug})
    assert workflow.source_images([4]) == [image]
    assert workflow.source_images([1]) == []
    assert store.get("source") == source
    assert store.get("spine") == spine and store.get("bundles") == bundles
    assert store.get("visual-candidates") == [4]
    assert store.get("visual-diagnostics")["pages"] == {"4": ["source map"]}
    assert store.usage()["call_count"] == 0
