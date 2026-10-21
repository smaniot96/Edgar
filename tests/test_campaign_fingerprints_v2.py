"""Bulk coverage checks retain hashes without repeatedly reading campaign artifacts."""

from collections import Counter
from copy import deepcopy

import pytest
from test_campaign_chapter_calls_v2 import pdf_store as pdf_store
from test_campaign_shared_profiles_v2 import fixture as shared_fixture

from apps.api.services.campaign_prep import graph
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.profiles import canonical_profiles
from apps.api.services.campaign_prep.store import digest


def legacy_fingerprint(workflow, registry, key):
    """Frozen pre-bulk algorithm: coverage already stored in Ready campaigns uses this."""
    deps = graph.dependencies(registry)
    deps["campaign_spine"].update(
        row["id"]
        for row in workflow.store.get("spine")["entities"] + workflow.store.get("spine")["secrets"]
    )
    needed, pending = set(), list(deps[key])
    while pending:
        ref = pending.pop()
        if ref == key or ref in needed:
            continue
        needed.add(ref)
        pending.extend(deps[ref] - needed)
    spine = workflow.store.get("spine")
    related = {
        bundle["chapter_id"]
        for bundle in workflow.store.get("bundles")["chapters"]
        if any(
            row["id"] == key
            for field in ("goals", "events", "new_entities", "new_secrets")
            for row in bundle[field]
        )
    }
    source = workflow.store.get("source") or {"pages": []}
    pages = {
        page
        for chapter in spine["chapters"]
        if chapter["id"] in related
        for page in chapter["pages"]
    }
    if key == "campaign_spine":
        pages.update(page["page"] for page in source["pages"])
    elif "origin" in registry[key]["value"]:
        pages.update(registry[key]["value"]["origin"]["pages"])
    return digest(
        {
            "visual_evidence": {
                str(page): workflow.store.get("visual-" + str(page)) for page in sorted(pages)
            },
            "record": registry[key],
            "dependencies": {ref: registry[ref] for ref in sorted(needed)},
            "campaign_context": {k: spine[k] for k in ("premise", "setting", "hook", "ending")},
            "chapter_context": [
                {
                    "chapter": chapter,
                    "dm_guidance": next(
                        bundle["dm_guidance"]
                        for bundle in workflow.store.get("bundles")["chapters"]
                        if bundle["chapter_id"] == chapter["id"]
                    ),
                }
                for chapter in spine["chapters"]
                if chapter["id"] in related
            ],
        }
    )


@pytest.fixture
def fingerprint_workflow(pdf_store):
    spine, bundles = shared_fixture()
    pdf_store.put("spine", spine)
    pdf_store.put("bundles", canonical_profiles(bundles))
    pdf_store.put("visual-1", {"page": 1, "interpretation": "A bridge."})
    pdf_store.put("visual-3", {"page": 3, "interpretation": "Shared appendix."})
    return Workflow(pdf_store, semantic=object())


@pytest.mark.parametrize("source_kind", ["pdf", "ai", "missing"])
@pytest.mark.parametrize("inline_legacy", [False, True])
def test_bulk_and_single_hashes_match_original_algorithm(
    fingerprint_workflow, source_kind, inline_legacy
):
    workflow = fingerprint_workflow
    if inline_legacy:
        _, bundles = shared_fixture()
        workflow.store.put("bundles", bundles)
    if source_kind != "pdf":
        workflow.store.put("source", {"kind": "ai", "pages": []} if source_kind == "ai" else None)
    registry = workflow.registry()
    original = {key: legacy_fingerprint(workflow, registry, key) for key in registry}
    assert workflow.fingerprints(registry) == original
    assert {key: workflow.fingerprint(registry, key) for key in registry} == original


def test_bulk_loads_shared_artifacts_dependencies_and_each_visual_once(
    fingerprint_workflow, monkeypatch
):
    workflow = fingerprint_workflow
    registry = workflow.registry()
    reads = Counter()
    get = workflow.store.get
    dependency_calls = []
    dependencies = graph.dependencies

    def counted_get(name, *args, **kwargs):
        reads[name] += 1
        return get(name, *args, **kwargs)

    def counted_dependencies(value):
        dependency_calls.append(value)
        return dependencies(value)

    monkeypatch.setattr(workflow.store, "get", counted_get)
    monkeypatch.setattr(graph, "dependencies", counted_dependencies)
    assert workflow.fingerprints(registry).keys() == registry.keys()
    assert reads == Counter(
        {"spine": 1, "bundles": 1, "source": 1, "visual-1": 1, "visual-2": 1, "visual-3": 1}
    )
    assert dependency_calls == [registry]


@pytest.mark.parametrize("change", ["profile", "guidance", "visual", "new_source_page"])
def test_each_new_calculation_observes_changed_inputs_with_original_invalidation(
    fingerprint_workflow, change
):
    workflow = fingerprint_workflow
    registry = workflow.registry()
    original_registry = deepcopy(registry)
    original = workflow.fingerprints(registry)
    bundles = workflow.store.get("bundles")
    if change == "profile":
        bundles["mechanical_profiles"][0]["source_stat_block"] += "\nCorrected trait."
        workflow.store.put("bundles", bundles)
    elif change == "guidance":
        bundles["chapters"][0]["dm_guidance"] += "\nThe bridge is unstable."
        workflow.store.put("bundles", bundles)
    elif change == "visual":
        workflow.store.put("visual-2", {"page": 2, "interpretation": "The bridge has a door."})
    else:
        source = workflow.store.get("source")
        source["pages"].append({"page": 4, "text": "An extra appendix."})
        workflow.store.put("source", source)
        workflow.store.put("visual-4", {"page": 4, "interpretation": "Appendix table."})
    assert registry == original_registry
    updated_registry = workflow.registry()
    updated = workflow.fingerprints(updated_registry)
    assert updated == {
        key: legacy_fingerprint(workflow, updated_registry, key) for key in updated_registry
    }
    changed = {key for key in registry if original[key] != updated[key]}
    if change == "profile":
        assert {"event_square", "event_after", "drake_a", "drake_b"} <= changed
        assert "ch_bell_content" not in changed and "place_square" not in changed
    elif change == "guidance":
        assert {"event_square", "goal_keeper", "campaign_spine"} <= changed
        assert "event_after" not in changed and "place_square" not in changed
    elif change == "visual":
        assert {"event_after", "campaign_spine"} <= changed
        assert "event_square" not in changed and "place_square" not in changed
    else:
        assert changed == {"campaign_spine"}
