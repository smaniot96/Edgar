"""Broken joins cannot silently publish orphan finales or unsupported completion."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_campaign_graph_v2 import sample

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep.compile import assemble
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.playability import (
    CompatibilityReview,
    capability_inventory,
    compatibility_findings,
    compatibility_signature,
    require_compatibility_review,
    topology,
)
from apps.api.services.campaign_prep.store import NeedsAttention, Store, digest


def campaign_with_orphan_finale():
    spine, bundles = deepcopy(sample())
    first = bundles["chapters"][0]["events"][0]
    finale = deepcopy(first)
    finale.update(id="event_finale", title="The escalating finale", next_event_ids=[])
    bundles["chapters"][0]["events"].append(finale)
    return spine, bundles


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["pdf", "ai"])
async def test_schema_valid_orphan_finale_routes_both_sources_into_semantic_repair(
    tmp_path, monkeypatch, source_kind
):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path)
    store = Store("topology_probe")
    store.initialize({"source_kind": source_kind, "rules_edition": "2014"})
    spine, bundles = campaign_with_orphan_finale()
    store.put("spine", spine)
    store.put("bundles", bundles)
    store.put("source", {"kind": source_kind, "pages": []})
    store.put("mechanics", [])
    workflow = Workflow(store, semantic=object())

    await workflow.assemble({"slug": store.slug})

    assert workflow.route_assembly({"slug": store.slug}) == "repair"
    finding = store.get("assembly-findings")[0]
    assert finding["severity"] == "material"
    assert "event_finale" in finding["evidence"]
    assert set(finding["record_ids"]) == {"event_square", "event_finale"}
    assert store.get("compiled") is None

    # A real source-grounded link correction, rather than inventory deletion,
    # clears the gate and preserves the finale in the executable manifest.
    bundles["chapters"][0]["events"][0]["next_event_ids"] = ["event_finale"]
    store.put("bundles", bundles)
    await workflow.assemble({"slug": store.slug})
    assert workflow.route_assembly({"slug": store.slug}) == "review"
    scenes = {row["scene_id"]: row for row in store.get("compiled")["manifest"]["scenes"]}
    assert [row["scene_id"] for row in scenes["event_square"]["links"]] == ["event_finale"]
    assert "event_finale" in scenes


def test_topology_keeps_conditional_return_routes_and_honest_viewpoints():
    spine, bundles = campaign_with_orphan_finale()
    opening, finale = bundles["chapters"][0]["events"]
    opening["next_event_ids"] = [finale["id"]]
    finale["next_event_ids"] = [opening["id"]]
    finale["trigger"] = "The hero calls from outside the closed door."
    finale["public_description"] = "Through the closed door you see the concealed prisoner."

    audit = topology(spine, bundles)

    assert audit["unreachable_event_ids"] == []
    assert audit["links"][finale["id"]] == [opening["id"]]
    pair = next(row for row in audit["event_viewpoints"] if row["event_id"] == finale["id"])
    assert pair["trigger"] == finale["trigger"]
    assert pair["public_description"] == finale["public_description"]


@pytest.fixture
def capability_case(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path)
    store = Store("compatibility_probe")
    store.initialize({"source_kind": "ai", "rules_edition": "2014"})
    spine, bundles = sample()
    compiled = assemble(store.slug, "2014", spine, bundles, [])
    compiled["capability_findings"] = [
        {"event_id": "event_square", "detail": "Restoration transformation has no executable use."}
    ]
    return store, compiled


def decision(compiled, **changes):
    value = {
        "capability_id": capability_inventory(compiled)[0]["capability_id"],
        "disposition": "blocked",
        "required_for_completion": True,
        "record_ids": ["event_square"],
        "evidence": "Required restoration cannot execute through custody alone.",
        "alternative_quote": "",
    }
    value.update(changes)
    return CompatibilityReview.model_validate({"decisions": [value]})


def test_missing_or_blocked_exact_capability_receipt_holds_publication(capability_case):
    store, compiled = capability_case
    with pytest.raises(NeedsAttention, match="incomplete or blocked"):
        require_compatibility_review(store, compiled)
    result = decision(compiled)
    findings = compatibility_findings(compiled, result)
    assert findings[0]["severity"] == "material"
    assert "custody" in findings[0]["instruction"]
    store.put("compatibility-review", result.model_dump(), compatibility_signature(compiled))
    with pytest.raises(NeedsAttention, match="incomplete or blocked"):
        require_compatibility_review(store, compiled)


def test_required_gap_cannot_be_dismissed_or_adaptation_fabricated(capability_case):
    _, compiled = capability_case
    with pytest.raises(NeedsAttention, match="cannot be classified as optional"):
        compatibility_findings(compiled, decision(compiled, disposition="optional"))
    with pytest.raises(NeedsAttention, match="exact existing authored evidence"):
        compatibility_findings(
            compiled,
            decision(
                compiled, disposition="supported_adaptation", alternative_quote="Invented fix"
            ),
        )
    with pytest.raises(NeedsAttention, match="every exact compiled capability gap once"):
        compatibility_findings(compiled, CompatibilityReview(decisions=[]))


def test_optional_gap_receipt_is_bound_to_exact_compilation_and_preserves_old_publication(
    capability_case,
):
    store, compiled = capability_case
    result = decision(compiled, disposition="optional", required_for_completion=False)
    store.put("compatibility-review", result.model_dump(), compatibility_signature(compiled))
    require_compatibility_review(store, compiled)
    compiled["semantic_sha256"] = "changed authored outcome"
    with pytest.raises(NeedsAttention, match="incomplete or blocked"):
        require_compatibility_review(store, compiled)
    # Old immutable publications keep their existing exact readiness contract.
    del compiled["compatibility_policy"]
    with pytest.raises(NeedsAttention, match="Unpublished legacy compilation"):
        require_compatibility_review(store, compiled)
    store.put("publication", {"compiled_sha256": digest(compiled)})
    require_compatibility_review(store, compiled)


@pytest.mark.asyncio
async def test_compatibility_review_replays_exact_receipt_without_another_paid_call(
    capability_case,
):
    store, compiled = capability_case
    spine, bundles = sample()
    store.put("spine", spine)
    store.put("bundles", bundles)
    calls = []

    async def call(node, prompt, schema, evidence):
        calls.append(evidence)
        assert node == prompt == "capability-review"
        assert schema is CompatibilityReview
        return decision(compiled, disposition="optional", required_for_completion=False)

    workflow = Workflow(store, semantic=SimpleNamespace(call=call))
    await workflow.review_compatibility(compiled)
    await workflow.review_compatibility(compiled)
    assert len(calls) == 1
    assert calls[0]["campaign"]["ending"] == spine["ending"]
    require_compatibility_review(store, compiled)
    compiled["semantic_sha256"] = "changed goal requires a fresh assessment"
    await workflow.review_compatibility(compiled)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_supported_engine_change_clears_stale_compatibility_blocker(capability_case):
    store, compiled = capability_case
    store.put(
        "findings",
        [
            {
                "owner": "compatibility",
                "value": compatibility_findings(compiled, decision(compiled))[0],
            }
        ],
    )
    compiled["capability_findings"] = []
    workflow = Workflow(store, semantic=object())
    await workflow.review_compatibility(compiled)
    assert store.get("findings") == []
    require_compatibility_review(store, compiled)


@pytest.mark.asyncio
async def test_cached_unpublished_legacy_compilation_refreshes_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path)
    store = Store("legacy_draft_probe")
    store.initialize({"source_kind": "ai", "rules_edition": "2014"})
    spine, bundles = sample()
    source = {"kind": "ai", "pages": []}
    store.put("spine", spine)
    store.put("bundles", bundles)
    store.put("source", source)
    store.put("mechanics", [])
    old_compiled = assemble(store.slug, "2014", spine, bundles, [], source=source)
    del old_compiled["compatibility_policy"]
    store.put("compiled", old_compiled, digest([spine, bundles, [], source]))
    await Workflow(store, semantic=object()).assemble({"slug": store.slug})
    assert store.get("compiled")["compatibility_policy"] == "required-capabilities-1"
