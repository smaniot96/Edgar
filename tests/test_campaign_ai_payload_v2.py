"""Actual brief, executable choices and retained finale survive shared repair."""

# Imported pytest fixtures intentionally share names with their test parameters.
# ruff: noqa: F811

from copy import deepcopy

import pytest
from test_campaign_ai_generation_v2 import (
    AiResponses,
    ai_store,  # noqa: F401
    cast,
    chapter,
    checked,
    outline,
    premise,
    workflow,
)

from apps.api.services.campaign_prep.ai_generation import (
    SIZE_PROFILES,
    chapter_bundle,
    check_content_contract,
    check_outline,
)
from apps.api.services.campaign_prep.ai_payload import ai_payload_findings, require_ai_payload
from apps.api.services.campaign_prep.publication import publish as publish_prepared
from apps.api.services.campaign_prep.store import NeedsAttention


def test_small_optional_combat_request_cannot_be_satisfied_by_zero_fights():
    value = outline()
    value["chapters"] = [value["chapters"][0], value["chapters"][2]]
    first, final = value["chapters"]
    first["entity_ids"] = [e["id"] for e in cast()["entities"]]
    final["reveals_secret_ids"] = [s["id"] for s in cast()["secrets"]]
    for plan in value["chapters"]:
        plan["combat_encounters"] = []
    _, unpromised = check_outline(value, cast(), SIZE_PROFILES["small"], {"combat": "unpromised"})
    assert unpromised == []
    _, requested = check_outline(value, cast(), SIZE_PROFILES["small"], {"combat": "requested"})
    assert requested == ["Plan 1-2 combat encounters in total; 0 were planned."]
    value["chapters"][0]["combat_encounters"] = outline()["chapters"][0]["combat_encounters"]
    _, provided = check_outline(value, cast(), SIZE_PROFILES["small"], {"combat": "requested"})
    assert provided == []


def test_explicit_noncombat_request_overrides_medium_encounter_default():
    value = outline()
    for plan in value["chapters"]:
        plan["combat_encounters"] = []
    _, problems = check_outline(value, cast(), SIZE_PROFILES["medium"], {"combat": "excluded"})
    assert problems == []


def test_content_contract_requires_exact_quotes_and_does_not_invent_a_user_prohibition():
    request = {
        "generation": {"theme": "Consequential nonviolent and combat choices"},
        "preferences": "",
    }
    contract = {
        "combat": "requested",
        "evidence": [{"source": "theme", "quote": "combat choices"}],
        "rationale": "Avoidable combat still requires an actual playable branch.",
    }
    assert check_content_contract(contract, request, premise())[1] == []
    invented = deepcopy(contract)
    invented["evidence"][0]["quote"] = "two dragons"
    assert "absent" in check_content_contract(invented, request, premise())[1][0]
    forbidden = {
        **contract,
        "combat": "excluded",
        "evidence": [{"source": "ending", "quote": premise()["ending"]}],
    }
    assert "Only an explicit user" in check_content_contract(forbidden, request, premise())[1][0]
    assert (
        check_content_contract(
            {"combat": "unpromised", "evidence": [], "rationale": "Social mystery."},
            {"generation": {"theme": None}, "preferences": None},
            premise(),
        )[1]
        == []
    )


@pytest.mark.parametrize("key", ["tactics", "morale", "alternatives"])
def test_encounters_need_concrete_operating_fields(key):
    plan = outline()["chapters"][0]
    draft = chapter("ch1_arrival", plan)
    draft["events"][3]["encounters"][0][key] = "  "
    _, problems = checked(draft, plan)
    assert problems == [f"Event ch1_arrival_event_3: encounter needs concrete {key} guidance."]


def test_finale_needs_distinct_conditional_outcomes_and_retains_every_field():
    plan = outline()["chapters"][0]
    draft = chapter("ch1_arrival", plan)
    draft["resolution_options"][1]["outcome"] = draft["resolution_options"][0]["outcome"]
    assert "two distinct" in checked(draft, plan, final=True)[1][0]
    draft = chapter("ch1_arrival", plan)
    assert checked(draft, plan, final=True)[1] == []
    notes = chapter_bundle("ch1_arrival", draft)["source_notes"]
    for option in draft["resolution_options"]:
        assert all(value in notes for value in option.values())


@pytest.mark.asyncio
async def test_independent_review_gets_original_brief_and_contract_fingerprints(ai_store):
    responses = AiResponses(ai_store)
    flow = workflow(ai_store, responses)
    await flow.run()
    reviews = [e for name, e in responses.evidence if name == "Review"]
    assert reviews and all(
        e["ai_request"]["request"] == ai_store.state()["request"]["generation"] for e in reviews
    )
    assert all(
        e["ai_request"]["content_contract"] == ai_store.get("ai-content-contract") for e in reviews
    )
    registry = flow.registry()
    before = flow.fingerprints(registry)
    contract = ai_store.get("ai-content-contract")
    ai_store.put("ai-content-contract", {**contract, "rationale": "Corrected semantic assessment."})
    assert all(before[key] != value for key, value in flow.fingerprints(registry).items())


@pytest.mark.asyncio
async def test_removed_actual_combat_or_finale_cannot_pass_direct_publication(ai_store):
    await workflow(ai_store, AiResponses(ai_store)).run()
    spine, bundles = ai_store.get("spine"), ai_store.get("bundles")
    ai_store.put(
        "ai-content-contract",
        {
            "combat": "requested",
            "evidence": [{"source": "theme", "quote": "combat choices"}],
            "rationale": "The branch requires executable encounters.",
        },
    )
    assert ai_payload_findings(ai_store, spine, bundles) == []
    removed = deepcopy(bundles)
    for bundle in removed["chapters"]:
        for event in bundle["events"]:
            event["encounters"] = []
    ai_store.put("bundles", removed)
    assert "zero executable" in ai_payload_findings(ai_store, spine, removed)[0]["evidence"]
    with pytest.raises(NeedsAttention, match="Requested AI payload"):
        publish_prepared(ai_store)
    # Even leaving one encounter must retain all promised chapter fights.
    removed["chapters"][0] = bundles["chapters"][0]
    assert any(
        "promised 1" in row["evidence"] for row in ai_payload_findings(ai_store, spine, removed)
    )
    ai_store.put("bundles", bundles)
    bundles["chapters"][-1]["source_notes"] = "The adventure ends."
    ai_store.put("bundles", bundles)
    with pytest.raises(NeedsAttention, match="conditional resolution"):
        require_ai_payload(ai_store)
