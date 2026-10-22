"""External rulebook locators never masquerade as physical adventure PDF pages."""

# Imported pytest fixtures intentionally share names with their test parameters.
# ruff: noqa: F811

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_campaign_structural_repair_v2 import recovery  # noqa: F401

from apps.api.services.campaign_prep.profile_references import (
    ProfilePageJoins,
    profile_reference_defects,
    reconcile_profile_pages,
)
from apps.api.services.campaign_prep.store import NeedsAttention


def profile_case(recovery, result=None):
    source = {
        "kind": "pdf",
        "pages": [
            {
                "page": 6,
                "text": "Gibbering Mouther (MM p157). Existing bespoke statistics.",
                "regions": [],
                "raw_sha256": "a" * 64,
                "image_sha256": "b" * 64,
                "width": 1000,
                "height": 1400,
                "image": "six.png",
                "quality": {},
            }
        ],
    }
    recovery.store.put("source", source)
    recovery.store.put(
        "mechanics",
        [
            {
                "name": "Gibbering Mouther",
                "text": "Gibbering Mouther\nArmor Class 9",
                "collection": "rules_monster_manual",
                "source_sha256": "c" * 64,
            }
        ],
    )
    bundles = recovery.store.get("bundles")
    bundles["mechanical_profiles"] = [
        {
            "id": "mouther_profile",
            "creature": "Gibbering Mouther",
            "source_stat_block": "Existing bespoke statistics.",
            "source_pages": [6, 158],
        }
    ]
    bundles["chapters"][0]["events"][0]["encounters"] = [
        {
            "profile_id": "mouther_profile",
            "actor_id": "noke_mutated",
            "quantity": 1,
            "adaptation": "Attack mindlessly.",
        }
    ]
    result = result or {
        "joins": [
            {
                "owner": "shared",
                "expected_id": "mouther_profile",
                "mode": "catalogue",
                "source_pages": [6],
                "evidence_quote": "Gibbering Mouther\nArmor Class 9",
                "rationale": "This intended unmodified creature uses the existing verified catalogue.",
            }
        ]
    }
    calls = []

    async def call(node, prompt, schema, evidence, images, **kwargs):
        assert schema is ProfilePageJoins and prompt == "profile-page-joins"
        calls.append(deepcopy(evidence))
        return schema.model_validate(result)

    recovery.semantic = SimpleNamespace(call=call)
    return bundles, calls, result


@pytest.mark.asyncio
async def test_bad_external_locator_can_explicitly_bind_existing_verified_template(recovery):
    bundles, calls, _ = profile_case(recovery)
    bundles["chapters"][0]["new_entities"] = [
        {
            "id": "noke_mutated",
            "kind": "npc",
            "name": "Noke",
            "description": "Noke's changed form.",
            "origin": {
                "kind": "pdf",
                "pages": [6, 158],
                "regions": [],
                "note": "Original evidence.",
                "uncertain": False,
            },
        }
    ]
    original = deepcopy(bundles)
    fixed = await reconcile_profile_pages(recovery, recovery.store.get("spine"), bundles)
    assert len(calls) == 1
    assert fixed["mechanical_profiles"][0] == {
        **original["mechanical_profiles"][0],
        "source_stat_block": "",
        "source_pages": [6],
    }
    assert "[158]" in fixed["chapters"][0]["source_notes"]
    assert "not verified pages of this PDF" in fixed["chapters"][0]["source_notes"]
    assert fixed["chapters"][0]["events"] == original["chapters"][0]["events"]
    actor = fixed["chapters"][0]["new_entities"][0]
    assert actor["origin"]["pages"] == [6]
    assert "[158]" in actor["origin"]["note"]
    assert {k: v for k, v in actor.items() if k != "origin"} == {
        k: v for k, v in original["chapters"][0]["new_entities"][0].items() if k != "origin"
    }
    assert bundles == original
    assert not profile_reference_defects(fixed, recovery.store.get("source"))
    assert await reconcile_profile_pages(recovery, recovery.store.get("spine"), bundles) == fixed
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_source_mode_preserves_bespoke_stats_with_actual_physical_evidence(recovery):
    bundles, calls, value = profile_case(recovery)
    value["joins"][0].update(mode="source", evidence_quote="Existing bespoke statistics.")
    fixed = await reconcile_profile_pages(recovery, recovery.store.get("spine"), bundles)
    assert fixed["mechanical_profiles"][0]["source_stat_block"] == "Existing bespoke statistics."
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["fake_page", "lost_valid_page", "fake_quote", "wrong_profile"])
async def test_unsupported_provenance_correction_holds_without_repayment(recovery, defect):
    bundles, calls, value = profile_case(recovery)
    join = value["joins"][0]
    if defect == "fake_page":
        join["source_pages"] = [6, 158]
    elif defect == "lost_valid_page":
        join["source_pages"] = []
    elif defect == "fake_quote":
        join["evidence_quote"] = "Invented verified statistics"
    else:
        join["expected_id"] = "other_profile"
    original = deepcopy(bundles)
    with pytest.raises(NeedsAttention, match="rejected"):
        await reconcile_profile_pages(recovery, recovery.store.get("spine"), bundles)
    with pytest.raises(NeedsAttention, match="Retained profile"):
        await reconcile_profile_pages(recovery, recovery.store.get("spine"), bundles)
    assert bundles == original and len(calls) == 1


@pytest.mark.asyncio
async def test_valid_provenance_avoids_extra_call(recovery):
    bundles, calls, _ = profile_case(recovery)
    bundles["mechanical_profiles"][0]["source_pages"] = [6]
    assert await reconcile_profile_pages(recovery, recovery.store.get("spine"), bundles) == bundles
    assert calls == []
