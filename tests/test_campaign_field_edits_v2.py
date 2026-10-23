"""Sparse prose repairs remove the defect while retaining every unrelated accepted byte."""

import json
from copy import deepcopy

import pytest
from test_campaign_graph_v2 import sample

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep.compile import records
from apps.api.services.campaign_prep.field_edits import (
    FieldEditBatch,
    apply_field_edits,
    recover_field_edits,
)
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.provider import OutputExhausted
from apps.api.services.campaign_prep.store import NeedsAttention, Store, digest
from edgar_core.campaign_semantics import Bundles, FieldEdit, Patch, Spine


def fixture():
    spine, bundles = sample()
    notes = "Keep the trail and its traps. Do not promise castle directions. Retain the rescue reward."
    bundles["chapters"][0]["source_notes"] = notes
    bundles["chapters"][0]["events"][0]["dm_notes"] = notes
    spine = Spine.model_validate(spine).model_dump()
    bundles = Bundles.model_validate(bundles).model_dump()
    return spine, bundles, deepcopy(records(spine, bundles))


@pytest.mark.parametrize("field,key", [
    ("source_notes", "ch_bell_content"), ("dm_notes", "event_square"),
    ("public_description", "event_square"), ("trigger", "event_square"), ("timing", "event_square"),
])
def test_exact_edit_removes_conflict_and_preserves_surrounding_reference(field, key):
    _, bundles, registry = fixture()
    if field not in {"source_notes", "dm_notes"}:
        text = registry["event_square"]["value"]["dm_notes"]
        bundles["chapters"][0]["events"][0][field] = text
        registry[key]["value"][field] = text
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
@pytest.mark.parametrize("outcome", ["merged", "still_overlaps", "wrong_field"])
async def test_overlap_consolidation_is_bounded_atomic_and_preserves_other_edits(
    tmp_path, monkeypatch, outcome,
):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path)
    store = Store("overlap_probe")
    store.initialize({"source_kind": "ai", "rules_edition": "2014"})
    _, bundles, registry = fixture()
    broad = FieldEdit(record_id="ch_bell_content", field="source_notes",
                      old_text="Do not promise castle directions.", new_text="A captive can give directions.")
    inner = broad.model_copy(update={"old_text": "promise castle directions.", "new_text": "deny directions."})
    unrelated = FieldEdit(record_id="event_square", field="dm_notes",
                          old_text="Retain the rescue reward.", new_text="Keep the earned reward.")
    proposed = [broad, inner, unrelated]
    raw = [row.model_dump() for row in proposed]
    before = deepcopy(bundles)
    calls = []

    class Consolidator:
        async def call(self, node, prompt, schema, evidence):
            calls.append(node)
            assert prompt == "field-edit-overlap" and schema is FieldEditBatch
            assert evidence["proposed_edits"] == raw[:2]
            assert bundles == before
            merged = broad.model_copy(update={"new_text": "A questioned captive can give castle directions."})
            edits = [merged]
            if outcome == "still_overlaps":
                edits = [broad, inner]
            elif outcome == "wrong_field":
                edits = [unrelated]
            return FieldEditBatch(field_edits=edits)

    if outcome == "merged":
        receipts = await recover_field_edits(
            registry, bundles, proposed, {"ch_bell_content", "event_square"}, Consolidator(), store,
        )
        assert len(receipts) == 2
        assert "A questioned captive can give castle directions." in bundles["chapters"][0]["source_notes"]
        assert "Keep the trail and its traps." in bundles["chapters"][0]["source_notes"]
        assert "Keep the earned reward." in bundles["chapters"][0]["events"][0]["dm_notes"]
        with store.db() as db:
            assert db.execute("SELECT count(*) FROM artifact WHERE name LIKE 'field-edit-consolidation-%'").fetchone()[0] == 1
    else:
        with pytest.raises(NeedsAttention):
            await recover_field_edits(
                registry, bundles, proposed, {"ch_bell_content", "event_square"}, Consolidator(), store,
            )
        assert bundles == before
    assert len(calls) == 1 and [row.model_dump() for row in proposed] == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("old_text", ["A sentence from another event.", " "])
@pytest.mark.parametrize("valid", [True, False])
async def test_unmatched_field_edits_are_rebased_once_without_guessing(
    tmp_path, monkeypatch, old_text, valid,
):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path)
    store = Store("rebase_probe")
    store.initialize({"source_kind": "ai", "rules_edition": "2014"})
    _, bundles, registry = fixture()
    before = deepcopy(bundles)
    retained = FieldEdit(record_id="ch_bell_content", field="source_notes",
                         old_text=old_text, new_text="A captive can give directions.")
    unrelated = FieldEdit(record_id="event_square", field="dm_notes",
                          old_text="Retain the rescue reward.", new_text="Keep the earned reward.")
    calls = []

    class Rebaser:
        async def call(self, node, prompt, schema, evidence):
            calls.append(node)
            assert prompt == "field-edit-rebase" and schema is FieldEditBatch
            assert evidence["original_text"] == before["chapters"][0]["source_notes"]
            assert evidence["proposed_edits"] == [retained.model_dump()]
            assert bundles == before
            corrected = retained.model_copy(update={"old_text": "Do not promise castle directions."})
            return FieldEditBatch(field_edits=[corrected if valid else retained])

    if valid:
        await recover_field_edits(
            registry, bundles, [retained, unrelated],
            {"ch_bell_content", "event_square"}, Rebaser(), store,
        )
        assert "A captive can give directions." in bundles["chapters"][0]["source_notes"]
        assert "Keep the trail and its traps." in bundles["chapters"][0]["source_notes"]
        assert "Keep the earned reward." in bundles["chapters"][0]["events"][0]["dm_notes"]
    else:
        with pytest.raises(NeedsAttention, match="remains invalid"):
            await recover_field_edits(
                registry, bundles, [retained, unrelated],
                {"ch_bell_content", "event_square"}, Rebaser(), store,
            )
        assert bundles == before
    assert len(calls) == 1 and retained.old_text == old_text


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


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["fixed", "empty", "duplicate", "outside", "exhausted"])
async def test_exhausted_patch_gets_one_finite_prose_recovery_and_normal_validation(
    tmp_path, monkeypatch, outcome,
):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    store = Store("exhausted_prose")
    store.initialize({"source_kind": "ai", "rules_edition": "2014"})
    spine, bundles, _ = fixture()
    event = bundles["chapters"][0]["events"][0]
    event["public_description"] = "The immediate danger has passed. The shelves remain crowded."
    for key, value in {"source": {"kind": "ai", "pages": []}, "spine": spine,
                       "bundles": bundles, "mechanics": [], "findings": [{"owner": "critic", "value": {
                           "id": "opening_assumption", "severity": "material", "record_ids": [event["id"]],
                           "evidence": "The trigger permits an unresolved threat.",
                           "instruction": "Make the opening neutral; preserve the threat's committed status.",
                       }}]}.items():
        store.put(key, value)
    response = {"id": "resp_original", "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"output_tokens": 128000}, "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": '{"events":[{"id":"unfinished'}]}]}
    original = OutputExhausted("parent", response)
    before = digest(response)
    calls = []

    class Repair:
        async def call(self, node, prompt, schema, evidence, images=None):
            calls.append(node)
            if node == "scoped-repair":
                assert schema is Patch
                raise original
            assert prompt == "repair-prose" and evidence["parent_task_id"] == "parent"
            assert list(evidence["affected_records"]) == [event["id"]]
            assert schema.model_json_schema()["properties"]["field_edits"]["maxItems"] == 4
            if outcome == "exhausted":
                raise OutputExhausted("recovery", response)
            edits = [{"record_id": event["id"], "field": "public_description",
                      "old_text": "The immediate danger has passed.",
                      "new_text": "The laboratory waits on your next decision."}]
            if outcome == "empty":
                edits = []
            elif outcome == "duplicate":
                edits *= 2
            elif outcome == "outside":
                edits[0]["record_id"] = "other_event"
            return schema(field_edits=edits)

    if outcome == "fixed":
        await Workflow(store, semantic=Repair()).repair({"slug": store.slug})
        expected = deepcopy(bundles)
        expected["chapters"][0]["events"][0]["public_description"] = (
            "The laboratory waits on your next decision. The shelves remain crowded."
        )
        assert store.get("bundles") == expected
        assert store.get("repair-history")  # unchanged-problem loop protection is still active
    else:
        with pytest.raises(NeedsAttention):
            await Workflow(store, semantic=Repair()).repair({"slug": store.slug})
        assert store.get("bundles") == bundles
    assert len(calls) == 2 and digest(response) == before
    assert store.get("repair-exhaustion-parent")["incomplete_patch_accepted"] is False
