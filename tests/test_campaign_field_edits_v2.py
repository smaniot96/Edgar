"""Sparse prose repairs remove the defect while retaining every unrelated accepted byte."""

import json
from copy import deepcopy

import pytest
from test_campaign_graph_v2 import sample

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep.compile import records
from apps.api.services.campaign_prep.field_edits import apply_field_edits
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.store import NeedsAttention, Store
from edgar_core.campaign_semantics import Bundles, FieldEdit, Patch, Spine


def fixture():
    spine, bundles = sample()
    notes = "Keep the trail and its traps. Do not promise castle directions. Retain the rescue reward."
    bundles["chapters"][0]["source_notes"] = notes
    bundles["chapters"][0]["events"][0]["dm_notes"] = notes
    spine = Spine.model_validate(spine).model_dump()
    bundles = Bundles.model_validate(bundles).model_dump()
    return spine, bundles, deepcopy(records(spine, bundles))


@pytest.mark.parametrize("field,key", [("source_notes", "ch_bell_content"), ("dm_notes", "event_square")])
def test_exact_edit_removes_conflict_and_preserves_surrounding_reference(field, key):
    _, bundles, registry = fixture()
    original = deepcopy(bundles)
    edit = FieldEdit(
        record_id=key, field=field, old_text="Do not promise castle directions.",
        new_text="A captured and threatened goblin can give castle directions.",
    )
    receipt = apply_field_edits(registry, bundles, [edit], {key})
    target = bundles["chapters"][0] if field == "source_notes" else bundles["chapters"][0]["events"][0]
    assert target[field] == (
        "Keep the trail and its traps. A captured and threatened goblin can give castle directions. "
        "Retain the rescue reward."
    )
    assert "Do not promise" not in target[field]
    assert receipt[0]["original_text"] == registry[key]["value"][field]
    assert receipt[0]["edits"] == [edit.model_dump()]
    target[field] = registry[key]["value"][field]
    assert bundles == original


@pytest.mark.parametrize("failure", ["missing", "ambiguous", "overlapping", "outside", "mixed", "wrong_kind"])
def test_invalid_field_edits_reject_atomically(failure):
    _, bundles, registry = fixture()
    key, field = "ch_bell_content", "source_notes"
    old_text = "Do not promise castle directions."
    allowed = {key}
    if failure == "missing":
        old_text = "Text that was never accepted."
    elif failure == "ambiguous":
        bundles["chapters"][0][field] = "aaaa"
        registry[key]["value"][field] = "aaaa"
        old_text = "aaa"  # Two overlapping occurrences must also count as ambiguous.
    elif failure == "outside":
        allowed = set()
    elif failure == "mixed":
        bundles["chapters"][0][field] += " Incidental replacement."
    elif failure == "wrong_kind":
        key = "event_square"
        allowed = {key}
    edits = [FieldEdit(record_id=key, field=field, old_text=old_text, new_text="Corrected.")]
    if failure == "overlapping":
        edits.append(FieldEdit(record_id=key, field=field, old_text="promise castle directions.", new_text=""))
    before = deepcopy(bundles)
    with pytest.raises(NeedsAttention):
        apply_field_edits(registry, bundles, edits, allowed)
    assert bundles == before


def test_invalid_second_record_keeps_first_record_unchanged():
    _, bundles, registry = fixture()
    edits = [
        FieldEdit(record_id="ch_bell_content", field="source_notes",
                  old_text="Do not promise castle directions.", new_text="Corrected guidance."),
        FieldEdit(record_id="event_square", field="dm_notes",
                  old_text="A phrase that is not present.", new_text=""),
    ]
    before = deepcopy(bundles)
    with pytest.raises(NeedsAttention, match="not found"):
        apply_field_edits(registry, bundles, edits, {"ch_bell_content", "event_square"})
    assert bundles == before


@pytest.mark.asyncio
async def test_workflow_sparse_repair_does_not_reintroduce_superseded_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    store = Store("sparse_prose")
    store.initialize({"source_kind": "ai", "rules_edition": "2014"})
    spine, bundles, _ = fixture()
    store.put("source", {"kind": "ai", "pages": []})
    store.put("spine", spine)
    store.put("bundles", bundles)
    store.put("mechanics", [])
    store.put("findings", [{"owner": "critic", "value": {
        "id": "captive_directions", "severity": "material", "record_ids": ["ch_bell_content"],
        "evidence": "Retained prose contradicts the established goblin knowledge.",
        "instruction": "Replace that instruction, preserving unrelated source detail.",
    }}])

    class Repair:
        async def call(self, node, prompt, schema, evidence, images=None):
            assert evidence["affected_records"]["ch_bell_content"]["value"]["source_notes"] == bundles["chapters"][0]["source_notes"]
            return Patch(field_edits=[FieldEdit(
                record_id="ch_bell_content", field="source_notes",
                old_text="Do not promise castle directions.",
                new_text="A captured and threatened goblin can give castle directions.",
            )])

    await Workflow(store, semantic=Repair()).repair({"slug": store.slug})
    repaired = store.get("bundles")
    assert "Do not promise" not in repaired["chapters"][0]["source_notes"]
    assert "Keep the trail and its traps." in repaired["chapters"][0]["source_notes"]
    assert "Retain the rescue reward." in repaired["chapters"][0]["source_notes"]
    repaired["chapters"][0]["source_notes"] = bundles["chapters"][0]["source_notes"]
    assert repaired == bundles
    with store.db() as db:
        receipt = json.loads(db.execute(
            "SELECT payload FROM artifact WHERE name LIKE 'repair-normalized-%'"
        ).fetchone()[0])
    assert receipt["field_edits"][0]["original_text"] == bundles["chapters"][0]["source_notes"]
    assert Patch.model_validate({"events": []}).field_edits == []
