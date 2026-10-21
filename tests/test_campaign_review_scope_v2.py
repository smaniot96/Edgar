"""Review packets distinguish missing campaign material from out-of-packet context."""

from copy import deepcopy

import pytest
from test_campaign_graph_v2 import ORIGIN, sample

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.store import Store
from edgar_core.campaign_semantics import Bundles, Review, Spine


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "side_compiled"),
    [
        ("campaign_spine", True),
        ("event_square", True),
        ("ch_bell_content", True),
        ("ch_bell_content", False),
    ],
)
async def test_review_packets_supply_complete_inventory_and_chapter_audit_context(
    tmp_path, monkeypatch, requested, side_compiled
):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path)
    store = Store("review_scope")
    store.initialize({"source_kind": "ai", "rules_edition": "2014"})
    spine, bundles = sample()
    actor = {
        "id": "npc_guard",
        "kind": "npc",
        "name": "Guard",
        "description": "An original guard.",
        "origin": ORIGIN,
    }
    spine["entities"].append(actor)
    side = deepcopy(bundles["chapters"][0]["events"][0])
    side.update(
        id="event_side",
        title="The keyed side room",
        participants=[actor["id"]],
        encounters=[
            {
                "actor_id": actor["id"],
                "creature": "Source Guard",
                "quantity": 3,
                "source_stat_block": "Complete retained source-specific profile.",
            }
        ],
    )
    bundles["chapters"][0]["events"].append(side)
    store.put("spine", Spine.model_validate(spine).model_dump())
    store.put("bundles", Bundles.model_validate(bundles).model_dump())
    store.put("source", {"kind": "ai", "pages": []})
    store.put(
        "compiled",
        {
            "manifest": {
                "scenes": [
                    {"scene_id": key}
                    for key in ("event_square", "event_side")
                    if key != "event_side" or side_compiled
                ],
                "objects": [],
                "resolution_policies": [],
            },
            "grounded": [{"event_id": "event_side", **side["encounters"][0]}]
            if side_compiled
            else [],
        },
    )
    captured = []

    class Critic:
        async def call(self, node, prompt, schema, evidence, images=None):
            assert prompt == "review" and schema is Review
            captured.append(evidence)
            return Review(covered_ids=evidence["review_ids"])

    workflow = Workflow(store, semantic=Critic())
    registry = workflow.registry()
    coverage = {key: workflow.fingerprint(registry, key) for key in registry if key != requested}
    store.put("review-coverage", coverage)
    await workflow.review({"slug": store.slug})
    assert len(captured) == 1
    evidence = captured[0]
    assert evidence["review_ids"] == [requested]
    assert evidence["campaign_record_index"]["event_side"] == "event"
    chapter = evidence["chapter_inventory"][0]
    assert chapter["chapter_id"] == "ch_bell"
    assert [event["id"] for event in chapter["events"]] == ["event_square", "event_side"]
    assert chapter["events"][1]["compiled_scene_present"] is side_compiled
    assert chapter["events"][1]["encounters"] == [
        {
            "actor_id": "npc_guard",
            "profile_id": None,
            "creature": "Source Guard",
            "quantity": 3,
            "source_stat_block_present": True,
            "compiled_binding_present": side_compiled,
        }
    ]
    if requested == "ch_bell_content":
        assert evidence["review_scope"]["complete_chapter_ids"] == ["ch_bell"]
        assert (
            evidence["dependencies"]["event_side"]["value"]["encounters"][0]["source_stat_block"]
            == "Complete retained source-specific profile."
        )
        expected_scenes = {"event_square", "event_side"} if side_compiled else {"event_square"}
        assert {scene["scene_id"] for scene in evidence["compiled_scenes"]} == expected_scenes
        if side_compiled:
            assert evidence["mechanics"][0]["event_id"] == "event_side"
        else:
            assert evidence["mechanics"] == []
        assert "npc_guard" in evidence["dependencies"]
    else:
        assert evidence["review_scope"]["complete_chapter_ids"] == []
        assert "event_side" not in evidence["dependencies"]
        assert all(scene["scene_id"] != "event_side" for scene in evidence["compiled_scenes"])
    assert evidence["review_scope"]["campaign_overview"] is (requested == "campaign_spine")
    assert store.get("review-coverage").keys() == registry.keys()
