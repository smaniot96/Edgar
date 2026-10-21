"""Recover valid paid rows without accepting a chapter disguised as an event."""

import json
from copy import deepcopy

import pytest
from test_campaign_chapter_calls_v2 import chapter_fixture, pdf_store as pdf_store

from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.store import NeedsAttention, digest
from edgar_core.campaign_semantics import Patch


def setup_repair(store):
    spine, bundles = chapter_fixture()
    store.put("spine", spine)
    store.put("bundles", bundles)
    store.put("findings", [{"owner": "critic", "value": {
        "id": "chapter_contradiction", "severity": "material",
        "record_ids": ["ch_bell_content"], "evidence": "The running note conflicts.",
        "instruction": "Correct the chapter note and affected event.",
    }}])
    proposal = deepcopy(bundles["chapters"][0])
    wrapper = deepcopy(proposal["events"][0])
    wrapper.update(
        id="ch_bell_content", location_id="ch_bell_content", goal_ids=[],
        public_description="", dm_notes="A rewritten chapter reference.",
    )
    proposal.update(events=[wrapper], new_entities=[], source_notes="Short correction only.")
    return bundles, proposal


@pytest.mark.asyncio
async def test_mistyped_chapter_is_quarantined_while_valid_event_patch_is_applied(pdf_store):
    bundles, proposal = setup_repair(pdf_store)
    corrected = deepcopy(bundles["chapters"][0]["events"][0])
    corrected["public_description"] = "An independently corrected sensory description."
    patch = Patch(chapters=[proposal], events=[corrected])
    immutable_proposal = patch.model_dump()

    class Repair:
        async def call(self, *_):
            return patch

    await Workflow(pdf_store, semantic=Repair()).repair({"slug": pdf_store.slug})
    accepted = pdf_store.get("bundles")
    assert accepted["chapters"][0]["source_notes"] == bundles["chapters"][0]["source_notes"]
    assert accepted["chapters"][0]["events"][0]["public_description"] == corrected["public_description"]
    accepted["chapters"][0]["events"][0]["public_description"] = bundles["chapters"][0]["events"][0]["public_description"]
    assert accepted == bundles
    assert patch.model_dump() == immutable_proposal
    with pdf_store.db() as db:
        receipt = json.loads(db.execute(
            "SELECT payload FROM artifact WHERE name LIKE 'repair-reconciled-%'"
        ).fetchone()[0])
    ignored = receipt["ignored_chapter_proposals"][0]
    assert ignored["proposal"] == proposal
    assert ignored["proposal_sha256"] == digest(proposal)
    assert ignored["retained_chapter_sha256"] == digest(bundles["chapters"][0])
    assert ignored["wrong_kind_records"] == [{
        "id": "ch_bell_content", "accepted_kind": "chapter", "proposed_kind": "event",
    }]
    assert pdf_store.get("findings")[0]["value"]["id"] == "chapter_contradiction"


@pytest.mark.asyncio
async def test_quarantine_without_independent_progress_remains_blocked(pdf_store):
    bundles, proposal = setup_repair(pdf_store)

    class Repair:
        async def call(self, *_):
            return Patch(chapters=[proposal])

    with pytest.raises(NeedsAttention, match="Repair made no progress"):
        await Workflow(pdf_store, semantic=Repair()).repair({"slug": pdf_store.slug})
    assert pdf_store.get("bundles") == bundles
    with pdf_store.db() as db:
        assert db.execute("SELECT count(*) FROM artifact WHERE name LIKE 'repair-reconciled-%'").fetchone()[0] == 1
    assert pdf_store.get("repair-history") is None


@pytest.mark.asyncio
async def test_unknown_new_event_is_rejected_instead_of_silently_quarantined(pdf_store):
    bundles, _ = setup_repair(pdf_store)
    proposal = deepcopy(bundles["chapters"][0])
    invented = deepcopy(proposal["events"][0])
    invented["id"] = "new_meaningful_event"
    proposal["events"].append(invented)

    class Repair:
        async def call(self, *_):
            return Patch(chapters=[proposal])

    with pytest.raises(NeedsAttention, match="changed contained record inventory"):
        await Workflow(pdf_store, semantic=Repair()).repair({"slug": pdf_store.slug})
    assert pdf_store.get("bundles") == bundles
    with pdf_store.db() as db:
        assert db.execute("SELECT count(*) FROM artifact WHERE name LIKE 'repair-reconciled-%'").fetchone()[0] == 0
