"""Scoped structural recovery retains paid authoring and never retries no-progress output."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_campaign_chapter_calls_v2 import chapter_fixture

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep.compile import validate_semantics
from apps.api.services.campaign_prep.provider import SemanticClient
from apps.api.services.campaign_prep.store import NeedsAttention, Store
from apps.api.services.campaign_prep.structural_repair import repair_structure, structural_defects
from edgar_core.campaign_semantics import Bundles


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "campaigns")
    store = Store("structural_probe")
    store.initialize({"source_kind": "ai", "rules_edition": "2014"})
    spine, bundles = chapter_fixture()
    for c in spine["chapters"]:
        c["pages"] = []
    spine["important_visual_pages"] = []
    for b in bundles["chapters"]:
        b["source_pages"] = []
    store.put("spine", spine)
    store.put("bundles", bundles)
    store.put("source", {"kind": "ai", "pages": []})
    calls = []

    async def call(node, prompt, schema, evidence, images):
        assert node == prompt == "structural-repair"
        assert schema is Bundles
        calls.append(deepcopy(evidence))
        return Bundles.model_validate(workflow.result)

    workflow = SimpleNamespace(
        store=store,
        semantic=SimpleNamespace(call=call),
        source_images=lambda _: [],
        result=None,
        calls=calls,
    )
    return workflow


def npc(identity):
    return {
        "id": identity,
        "kind": "npc",
        "name": identity,
        "aliases": [],
        "description": "A source participant.",
        "motive": "Protect the tower.",
        "knowledge": "",
        "origin": {"kind": "authored", "pages": [], "regions": [], "note": "", "uncertain": False},
    }


def broken(workflow):
    bundles = workflow.store.get("bundles")
    first = bundles["chapters"][0]
    event = first["events"][0]
    first["new_entities"].append(npc(event["id"]))
    event["encounters"] = [
        {
            "actor_id": event["id"],
            "creature": "Wolf",
            "quantity": 2,
            "adaptation": "",
            "source_stat_block": "",
            "source_pages": [],
        }
    ]
    event["next_event_ids"] = ["missing_event"]
    event["reveals"] = [
        {
            "entity_id": "place_square",
            "public_text": "The tower was once a home.",
            "condition": "Discover its history by a credible approach.",
        }
    ]
    workflow.store.put("bundles", bundles)
    result = deepcopy(first)
    event = result["events"][0]
    event["id"] = "event_square_arrival"
    event["next_event_ids"] = []
    event["participants"] = ["event_square"]
    event["dm_notes"] += "\n" + "\n".join(
        r["public_text"] + " " + r["condition"] for r in event["reveals"]
    )
    event["reveals"] = []
    result["goals"][0]["event_ids"] = [event["id"]]
    workflow.result = {"chapters": [result]}
    return bundles


@pytest.mark.asyncio
async def test_valid_structural_path_makes_no_call(recovery):
    assert not await repair_structure(recovery)
    assert recovery.calls == []


@pytest.mark.asyncio
async def test_collision_references_participants_and_remote_reveal_in_one_scoped_call(recovery):
    original = broken(recovery)
    assert await repair_structure(recovery)
    accepted = recovery.store.get("bundles")
    validate_semantics(recovery.store.get("spine"), accepted, recovery.store.get("source"))
    assert len(recovery.calls) == 1
    assert accepted["chapters"][1] == original["chapters"][1]
    first = accepted["chapters"][0]
    assert first["source_notes"] == original["chapters"][0]["source_notes"]
    assert first["new_entities"] == original["chapters"][0]["new_entities"]
    assert first["events"][0]["encounters"] == original["chapters"][0]["events"][0]["encounters"]
    assert not await repair_structure(recovery)
    assert len(recovery.calls) == 1
    with recovery.store.db() as db:
        saved = [
            row["name"]
            for row in db.execute("SELECT name FROM artifact")
            if row["name"].startswith("structural-repair-")
        ]
    history = recovery.store.get(saved[0])
    assert history["original_bundles"] == original
    assert history["state"] == "accepted"


@pytest.mark.asyncio
async def test_missing_actor_can_add_only_requested_identity(recovery):
    bundles = recovery.store.get("bundles")
    first = bundles["chapters"][0]
    first["events"][0]["encounters"] = [
        {
            "actor_id": "wolf_guard",
            "creature": "Wolf",
            "quantity": 1,
            "adaptation": "",
            "source_stat_block": "",
            "source_pages": [],
        }
    ]
    recovery.store.put("bundles", bundles)
    candidate = deepcopy(first)
    candidate["new_entities"].append(npc("wolf_guard"))
    candidate["events"][0]["participants"].append("wolf_guard")
    recovery.result = {"chapters": [candidate]}
    assert await repair_structure(recovery)
    assert recovery.calls[0]["allowed_new_actors"] == [
        {"chapter_id": "ch_bell", "id": "wolf_guard"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "unrelated_row",
        "unrelated_chapter",
        "mechanics",
        "lost_reveal",
        "no_progress",
        "invalid_reference",
    ],
)
async def test_invalid_repair_preserves_bundles_and_holds_on_repeat(recovery, bad):
    original = broken(recovery)
    first = recovery.result["chapters"][0]
    if bad == "unrelated_row":
        first["new_entities"][0]["motive"] = "Changed motive"
    elif bad == "unrelated_chapter":
        recovery.result["chapters"].append(original["chapters"][1])
    elif bad == "mechanics":
        first["events"][0]["encounters"][0]["quantity"] = 99
    elif bad == "lost_reveal":
        first["events"][0]["dm_notes"] = original["chapters"][0]["events"][0]["dm_notes"]
    elif bad == "no_progress":
        recovery.result = {"chapters": [deepcopy(original["chapters"][0])]}
    elif bad == "invalid_reference":
        first["events"][0]["next_event_ids"] = ["still_missing"]
    with pytest.raises(NeedsAttention, match="rejected"):
        await repair_structure(recovery)
    assert recovery.store.get("bundles") == original
    with pytest.raises(NeedsAttention, match="not resubmitted"):
        await repair_structure(recovery)
    assert len(recovery.calls) == 1


def test_preflight_preserves_all_defects_before_registry_collision(recovery):
    original = broken(recovery)
    issues, permissions, _ = structural_defects(recovery.store.get("spine"), original)
    assert {i["issue"] for i in issues} == {
        "identity_collision",
        "dangling_next_event",
        "encounter_outside_participants",
        "invalid_reveal_reference",
    }
    assert permissions[("ch_bell", "goals", 0)] == {"event_ids"}
    assert not any(key[0] == "ch_after" for key in permissions)


@pytest.mark.asyncio
async def test_paid_repair_response_replays_after_aggregate_acceptance_is_lost(recovery):
    original = broken(recovery)
    requests = []

    class Responses:
        async def create(self, **request):
            requests.append(request)
            assert request["model"] == "gpt-6-luna"
            assert request["reasoning"] == {"effort": "medium"}
            response = {
                "id": "resp_structural_repair_probe",
                "status": "completed",
                "model": "gpt-6-luna",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(recovery.result)}],
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
            return SimpleNamespace(model_dump=lambda **_: response)

    recovery.semantic = SemanticClient(recovery.store, SimpleNamespace(responses=Responses()))
    assert await repair_structure(recovery)
    # Crash after paid acceptance but before the derived aggregate/history update.
    recovery.store.put("bundles", original)
    with recovery.store.db() as db:
        db.execute("DELETE FROM artifact WHERE name LIKE 'structural-repair-%'")
    assert await repair_structure(recovery)
    assert len(requests) == 1
    assert recovery.store.usage()["call_count"] == 1
