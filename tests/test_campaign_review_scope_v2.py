"""Review packets distinguish missing campaign material from out-of-packet context."""

import json
from copy import deepcopy

import pytest
from test_campaign_graph_v2 import ORIGIN, sample

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.store import NeedsAttention, Store, digest
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


def partial_review_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path)
    store = Store("partial_review")
    store.initialize({"source_kind": "ai", "rules_edition": "2014"})
    spine, bundles = sample()
    for number in range(64):
        spine["entities"].append(
            {
                "id": "review_record_" + str(number),
                "kind": "npc",
                "name": "Person " + str(number),
                "description": "A distinct campaign actor.",
                "origin": ORIGIN,
            }
        )
    missing = "profile_b4756ca1d0f679377580ee81575f3e828729457e1c06025a"
    typo = "profile_b4756ca1d0f679377580ee81575f3e828729457a750c030b02ff2712"
    bundles["mechanical_profiles"] = [
        {"id": missing, "creature": "Wolf", "source_stat_block": "", "source_pages": []}
    ]
    store.put("spine", Spine.model_validate(spine).model_dump())
    store.put("bundles", Bundles.model_validate(bundles).model_dump())
    store.put("source", {"kind": "ai", "pages": []})
    store.put(
        "compiled",
        {
            "manifest": {
                "scenes": [{"scene_id": "event_square"}],
                "objects": [],
                "resolution_policies": [],
            },
            "grounded": [],
        },
    )
    prepared = Workflow(store)
    registry = prepared.registry()
    requested = {"review_record_" + str(number) for number in range(64)} | {missing}
    store.put(
        "review-coverage",
        {key: prepared.fingerprint(registry, key) for key in registry if key not in requested},
    )
    return store, requested, missing, typo


@pytest.mark.asyncio
async def test_partial_exact_coverage_rechecks_only_missing_id_and_preserves_unreviewed_findings(
    tmp_path, monkeypatch
):
    store, requested, missing, typo = partial_review_fixture(tmp_path, monkeypatch)
    prior = {
        "owner": digest(sorted(requested)),
        "value": {
            "id": "cross_record_advice",
            "severity": "advice",
            "record_ids": ["review_record_0", missing],
            "evidence": "Check the shared reference.",
            "instruction": "Check both records.",
        },
    }
    store.put("findings", [prior])
    calls = []

    class Critic:
        async def call(self, node, prompt, schema, evidence, images=None):
            calls.append(deepcopy(evidence))
            if len(calls) == 1:
                assert set(evidence["review_ids"]) == requested
                return Review(covered_ids=sorted(requested - {missing}) + [typo])
            assert evidence["review_ids"] == [missing]
            assert set(evidence["dependencies"]) == {"review_record_0"}
            assert evidence["previous_findings"] == [prior["value"]]
            return Review(covered_ids=[missing])

    prepared = Workflow(store, semantic=Critic())
    await prepared.review({"slug": store.slug})
    coverage = store.get("review-coverage")
    assert requested - coverage.keys() == {missing}
    assert typo not in coverage
    assert store.get("findings") == [prior]
    assert prepared.route_review({"slug": store.slug}) == "review"
    with store.db() as db:
        receipt = json.loads(
            db.execute(
                "SELECT payload FROM artifact WHERE name LIKE 'review-coverage-reconciled-%'"
            ).fetchone()[0]
        )
    assert len(receipt["accepted_ids"]) == 64
    assert receipt["missing_ids"] == [missing]
    assert receipt["extra_ids"] == [typo]
    await prepared.review({"slug": store.slug})
    assert len(calls) == 2
    assert store.get("findings") == []
    assert prepared.route_review({"slug": store.slug}) == "publish"
    # The conditional graph must continue review rather than attempt publication.
    assert any(
        edge.source == "review" and edge.target == "review"
        for edge in prepared.graph.get_graph().edges
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_finding", [False, True])
async def test_review_with_only_unexpected_coverage_holds_without_false_acceptance(
    tmp_path, monkeypatch, invalid_finding
):
    store, requested, missing, typo = partial_review_fixture(tmp_path, monkeypatch)
    before = store.get("review-coverage")
    calls = []

    class Critic:
        async def call(self, *args):
            calls.append(args)
            findings = (
                [
                    {
                        "id": "bad_join",
                        "severity": "material",
                        "record_ids": [typo],
                        "evidence": "An invented ID.",
                        "instruction": "Repair.",
                    }
                ]
                if invalid_finding
                else []
            )
            return Review(covered_ids=[typo], findings=findings)

    prepared = Workflow(store, semantic=Critic())
    with pytest.raises(
        NeedsAttention, match="nonexistent records" if invalid_finding else "no coverage progress"
    ):
        await prepared.review({"slug": store.slug})
    assert len(calls) == 1
    assert store.get("review-coverage") == before
    assert store.get("findings") is None
    if not invalid_finding:
        with store.db() as db:
            receipt = json.loads(
                db.execute(
                    "SELECT payload FROM artifact WHERE name LIKE 'review-coverage-reconciled-%'"
                ).fetchone()[0]
            )
        assert receipt["accepted_ids"] == [] and receipt["newly_covered_ids"] == []
        assert set(receipt["missing_ids"]) == requested
