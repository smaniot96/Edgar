"""Scoped structural recovery retains paid authoring and never retries no-progress output."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_campaign_chapter_calls_v2 import chapter_fixture

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep.compile import validate_semantics
from apps.api.services.campaign_prep.provider import (
    OutputExhausted,
    SemanticClient,
    instructions,
    response_format,
)
from apps.api.services.campaign_prep.store import NeedsAttention, Store, digest
from apps.api.services.campaign_prep.structural_repair import (
    StructuralJoins,
    StructuralLinks,
    _repair_evidence,
    repair_structure,
    structural_defects,
)
from edgar_core.campaign_semantics import SCHEMA_VERSION, Bundles


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

    async def call(node, prompt, schema, evidence, images, **kwargs):
        assert node == prompt == "structural-joins"
        assert schema is StructuralJoins
        calls.append(deepcopy(evidence))
        result = workflow.result
        if "chapters" in result:
            result = sparse_patch(evidence, result)
        return schema.model_validate(result)

    workflow = SimpleNamespace(
        store=store,
        semantic=SimpleNamespace(call=call),
        source_images=lambda _: [],
        result=None,
        calls=calls,
    )
    return workflow


def sparse_patch(evidence, replacement):
    """Represent test proposals as joins, without asking a fake model to copy prose."""
    old = {b["chapter_id"]: b for b in evidence["chapters"]}
    proposed = {b["chapter_id"]: b for b in replacement["chapters"]}
    rows, actors = [], []
    for permission in evidence["allowed_fields"]:
        cid, field, index = (permission[k] for k in ("chapter_id", "field", "index"))
        if cid not in proposed:
            continue
        before, after = old[cid][field][index], proposed[cid][field][index]
        edit = {
            "chapter_id": cid,
            "field": field,
            "index": index,
            "expected_id": before["entity_id" if field == "items" else "id"],
            "changes": [],
            "actors": [],
            "reveals": [],
        }
        for key in permission["keys"]:
            if key == "encounters":
                edit["actors"] = [
                    {"index": i, "actor_id": b["actor_id"]}
                    for i, (a, b) in enumerate(zip(before[key], after[key], strict=True))
                    if a["actor_id"] != b["actor_id"]
                ]
            elif key == "reveals":
                for i, reveal in enumerate(before[key]):
                    match = next(
                        (
                            r
                            for r in after[key]
                            if all(r[k] == reveal[k] for k in ("public_text", "condition"))
                        ),
                        None,
                    )
                    if match is None or match["entity_id"] != reveal["entity_id"]:
                        edit["reveals"].append(
                            {"index": i, "entity_id": match["entity_id"] if match else None}
                        )
            elif key != "dm_notes" and before[key] != after[key]:
                edit["changes"].append({"key": key, "value": after[key]})
        if any(edit[k] for k in ("changes", "actors", "reveals")):
            rows.append(edit)
    for permitted in evidence["allowed_new_actors"]:
        for entity in proposed.get(permitted["chapter_id"], {}).get("new_entities", []):
            if entity["id"] == permitted["id"]:
                actors.append({"chapter_id": permitted["chapter_id"], "entity": entity})
    for cid in proposed.keys() - old.keys():
        rows.append(
            {
                "chapter_id": cid,
                "field": "events",
                "index": 0,
                "expected_id": proposed[cid]["events"][0]["id"],
            }
        )
    return {"rows": rows, "new_actors": actors}


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
        "unrelated_chapter",
        "no_progress",
        "invalid_reference",
    ],
)
async def test_invalid_repair_preserves_bundles_and_holds_on_repeat(recovery, bad):
    original = broken(recovery)
    first = recovery.result["chapters"][0]
    if bad == "unrelated_chapter":
        recovery.result["chapters"].append(original["chapters"][1])
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


@pytest.mark.asyncio
async def test_repair_projects_joins_without_copying_unrelated_prose_or_mechanics(recovery):
    original = broken(recovery)
    first = recovery.result["chapters"][0]
    first["dm_guidance"] = "Unrequested rewrite"
    first["new_entities"][0]["motive"] = "Unrequested motive"
    first["events"][0]["encounters"][0]["quantity"] = 99
    assert await repair_structure(recovery)
    accepted = recovery.store.get("bundles")["chapters"][0]
    assert accepted["dm_guidance"] == original["chapters"][0]["dm_guidance"]
    assert accepted["new_entities"] == original["chapters"][0]["new_entities"]
    assert accepted["events"][0]["encounters"] == original["chapters"][0]["events"][0]["encounters"]


@pytest.mark.asyncio
async def test_reprocess_old_paid_repair_retains_discovery_wording_locally(recovery):
    original = broken(recovery)
    # A legacy renamed row has no unambiguous stable identity to project. Recover
    # only discovery joins in this case; collision repair is covered separately.
    original["chapters"][0]["new_entities"] = []
    original["chapters"][0]["events"][0]["encounters"] = []
    original["chapters"][0]["events"][0]["next_event_ids"] = []
    recovery.store.put("bundles", original)
    first = recovery.result["chapters"][0]
    first["events"][0]["id"] = original["chapters"][0]["events"][0]["id"]
    first["events"][0]["participants"] = original["chapters"][0]["events"][0]["participants"]
    first["goals"][0]["event_ids"] = [first["events"][0]["id"]]
    first["events"][0]["dm_notes"] = original["chapters"][0]["events"][0]["dm_notes"]
    issues, _, _ = structural_defects(recovery.store.get("spine"), original)
    from apps.api.services.campaign_prep.store import digest

    signature = digest(
        {"spine": recovery.store.get("spine"), "bundles": original, "issues": issues}
    )
    recovery.store.put(
        "structural-repair-" + signature,
        {
            "state": "rejected",
            "candidate": recovery.result,
            "original_bundles": original,
            "error": "Old copier lost literal discovery text.",
        },
    )
    assert await repair_structure(recovery)
    assert not recovery.calls
    accepted = recovery.store.get("bundles")["chapters"][0]["events"][0]
    reveal = original["chapters"][0]["events"][0]["reveals"][0]
    assert reveal["public_text"] in accepted["dm_notes"]
    assert reveal["condition"] in accepted["dm_notes"]


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
            evidence = json.loads(request["input"][1]["content"][0]["text"])
            patch = sparse_patch(evidence, recovery.result)
            response = {
                "id": "resp_structural_repair_probe",
                "status": "completed",
                "model": "gpt-6-luna",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(patch)}],
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


def legacy_dispatch(workflow, status, candidate=None):
    spine, bundles, source = (workflow.store.get(k) for k in ("spine", "bundles", "source"))
    issues, permissions, missing = structural_defects(spine, bundles)
    evidence, _ = _repair_evidence(workflow, spine, bundles, source, issues, permissions, missing)
    request = {
        "model": "gpt-6-luna",
        "reasoning": {"effort": "medium"},
        "input": [
            {"role": "system", "content": instructions("structural-repair")},
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    }
                ],
            },
        ],
        "text": {"format": response_format(Bundles)},
        "background": True,
        "store": True,
        "service_tier": "default",
    }
    task_id = digest(
        {"node": "structural-repair", "request": request, "schema_version": SCHEMA_VERSION}
    )
    workflow.store.dispatch(task_id, "structural-repair", request)
    response = {"id": "resp_retained_full", "status": status, "model": "gpt-6-luna"}
    if candidate is not None:
        response["output"] = [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(candidate)}]}
        ]
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    workflow.store.response(task_id, response)
    return task_id, response


@pytest.mark.asyncio
async def test_retained_incomplete_full_response_holds_without_sparse_dispatch(recovery):
    original = broken(recovery)
    task_id, _ = legacy_dispatch(recovery, "incomplete")
    paid_row = recovery.store.task(task_id)
    for _ in range(2):
        with pytest.raises(OutputExhausted):
            await repair_structure(recovery)
    assert recovery.calls == []
    assert recovery.store.task(task_id) == paid_row
    assert recovery.store.get("bundles") == original


@pytest.mark.asyncio
async def test_retained_inflight_full_response_is_retrieved_without_new_create(
    recovery, monkeypatch
):
    original = recovery.store.get("bundles")
    first = original["chapters"][0]
    first["events"][0]["reveals"] = [
        {
            "entity_id": "place_square",
            "public_text": "A remote discovery.",
            "condition": "A credible approach.",
        }
    ]
    recovery.store.put("bundles", original)
    candidate = deepcopy(first)
    candidate["events"][0]["reveals"] = []
    _, response = legacy_dispatch(recovery, "in_progress")
    calls = []

    class Responses:
        async def create(self, **request):
            pytest.fail("An existing full-output dispatch must never be recreated.")

        async def retrieve(self, response_id):
            calls.append(response_id)
            complete = {
                **response,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps({"chapters": [candidate]})}
                        ],
                    }
                ],
            }
            return SimpleNamespace(model_dump=lambda **_: complete)

    async def no_sleep(_):
        pass

    monkeypatch.setattr("apps.api.services.campaign_prep.provider.asyncio.sleep", no_sleep)
    recovery.semantic = SemanticClient(recovery.store, SimpleNamespace(responses=Responses()))
    assert await repair_structure(recovery)
    assert calls == ["resp_retained_full"]
    assert recovery.store.usage()["call_count"] == 1


@pytest.mark.asyncio
async def test_retained_subset_projects_stable_ids_and_only_repairs_residual(recovery):
    original = recovery.store.get("bundles")
    first = original["chapters"][0]
    first["events"][0]["reveals"] = [
        {
            "entity_id": "place_square",
            "public_text": "The tower was once a home.",
            "condition": "Learn its history by a credible approach.",
        }
    ]
    second = deepcopy(first["events"][0])
    second.update(
        id="event_later",
        reveals=[],
        encounters=[
            {
                "actor_id": "wolf_guard",
                "creature": "Wolf",
                "quantity": 2,
                "adaptation": "",
                "source_stat_block": "",
                "source_pages": [],
            }
        ],
    )
    first["events"].append(second)
    recovery.store.put("bundles", original)
    retained = deepcopy(first)
    retained["events"] = [deepcopy(second)]
    retained["events"][0]["participants"].append("wolf_guard")
    retained["new_entities"] = [npc("wolf_guard")]
    task_id, _ = legacy_dispatch(recovery, "completed", {"chapters": [retained]})
    paid_row = recovery.store.task(task_id)
    recovery.result = {
        "rows": [
            {
                "chapter_id": first["chapter_id"],
                "field": "events",
                "index": 0,
                "expected_id": first["events"][0]["id"],
                "reveals": [{"index": 0, "entity_id": None}],
            }
        ],
        "new_actors": [],
    }
    assert await repair_structure(recovery)
    assert recovery.calls[0]["allowed_new_actors"] == []
    assert [i["issue"] for i in recovery.calls[0]["defects"]] == ["invalid_reveal_reference"]
    accepted = recovery.store.get("bundles")["chapters"][0]
    assert accepted["events"][1]["id"] == "event_later"
    assert accepted["events"][1]["encounters"] == second["encounters"]
    assert accepted["new_entities"][-1] == npc("wolf_guard")
    assert recovery.store.task(task_id) == paid_row


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["identity", "permission", "duplicate", "nested_index"])
async def test_sparse_patch_rejects_unsafe_edits_atomically(recovery, bad):
    original = broken(recovery)
    issues, permissions, missing = structural_defects(recovery.store.get("spine"), original)
    evidence, _ = _repair_evidence(
        recovery,
        recovery.store.get("spine"),
        original,
        recovery.store.get("source"),
        issues,
        permissions,
        missing,
    )
    patch = sparse_patch(evidence, recovery.result)
    event = next(row for row in patch["rows"] if row["field"] == "events")
    if bad == "identity":
        event["expected_id"] = "wrong_identity"
    elif bad == "permission":
        event["changes"].append({"key": "secret_ids", "value": []})
    elif bad == "duplicate":
        patch["rows"].append(deepcopy(event))
    else:
        event["actors"] = [{"index": 999, "actor_id": "event_square"}]
    recovery.result = patch
    with pytest.raises(NeedsAttention, match="rejected"):
        await repair_structure(recovery)
    assert recovery.store.get("bundles") == original


@pytest.mark.asyncio
async def test_sparse_output_does_not_copy_large_accepted_source_or_profiles(recovery):
    original = broken(recovery)
    first = original["chapters"][0]
    first["source_notes"] = "Detailed source evidence and tactics. " * 10_000
    first["events"][0]["encounters"][0]["source_stat_block"] = "Exact bespoke profile. " * 5_000
    recovery.store.put("bundles", original)
    recovery.store.put("source", {"kind": "pdf", "pages": []})
    assert await repair_structure(recovery)
    accepted = recovery.store.get("bundles")["chapters"][0]
    assert accepted["source_notes"] == first["source_notes"]
    assert accepted["events"][0]["encounters"] == first["events"][0]["encounters"]
    patch = sparse_patch(recovery.calls[0], recovery.result)
    assert len(json.dumps(patch)) < 1_500
    assert "source_notes" not in StructuralJoins.model_json_schema()["properties"]


@pytest.mark.asyncio
async def test_all_reference_repairs_preserve_npcs_procedures_origins_and_paid_replay(recovery):
    recovery.store.put("source", {"kind": "pdf", "pages": []})
    original = recovery.store.get("bundles")
    first = original["chapters"][0]
    carrier = npc("carrier")
    carrier["origin"].update(regions=["Area 1"], note="Accepted carrier origin.")
    first["new_entities"].append(carrier)
    first["events"][0]["participants"].extend(["carrier", "source_cache"])
    first["events"][0]["clue_ids"].append("source_cache")
    origin = {
        "kind": "authored",
        "pages": [],
        "regions": ["Treasure section"],
        "note": "Accepted item origin.",
        "uncertain": False,
    }
    first["items"] = [
        {
            "entity_id": identity,
            "procedure": procedure,
            "event_ids": [first["events"][0]["id"]],
            "portable": True,
            "consumable": False,
            "origin": deepcopy(origin),
        }
        for identity, procedure in (
            ("carrier", "Boots of springing. Preserve every accepted number and condition."),
            ("missing_potion", "A healing potion. Preserve its complete accepted procedure."),
        )
    ]
    recovery.store.put("bundles", original)
    requests = []

    class Responses:
        async def create(self, **request):
            requests.append(request)
            assert request["text"]["format"]["name"] == StructuralLinks.__name__
            evidence = json.loads(request["input"][1]["content"][0]["text"])
            patch = {"rows": [], "items": [], "new_entities": []}
            for issue in evidence["defects"]:
                if issue["issue"] == "invalid_item_entity":
                    patch["items"].append(
                        {
                            "chapter_id": issue["chapter_id"],
                            "index": issue["index"],
                            "expected_id": issue["identity"],
                            "entity_id": issue["permitted_object_id"],
                        }
                    )
            for allowed in evidence["allowed_new_entities"]:
                entity = npc(allowed["id"])
                entity.update(kind="clue", name="Source object " + allowed["id"])
                entity["origin"]["regions"] = ["Original source object label"]
                patch["new_entities"].append(
                    {"chapter_id": allowed["chapter_id"], "entity": entity}
                )
            response = {
                "id": "resp_all_reference_probe",
                "status": "completed",
                "model": "gpt-6-luna",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(patch)}],
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
            return SimpleNamespace(model_dump=lambda **_: response)

    recovery.semantic = SemanticClient(recovery.store, SimpleNamespace(responses=Responses()))
    assert await repair_structure(recovery)
    accepted = recovery.store.get("bundles")
    validate_semantics(recovery.store.get("spine"), accepted, recovery.store.get("source"))
    changed = accepted["chapters"][0]
    retained_carrier = next(row for row in changed["new_entities"] if row["id"] == "carrier")
    assert retained_carrier["kind"] == "npc"
    assert retained_carrier["description"] == carrier["description"]
    assert retained_carrier["origin"]["note"].startswith(carrier["origin"]["note"])
    assert "Area 1" in retained_carrier["origin"]["note"]
    assert retained_carrier["origin"]["regions"] == []
    for before, after in zip(first["items"], changed["items"], strict=True):
        assert {k: v for k, v in before.items() if k not in ("entity_id", "origin")} == {
            k: v for k, v in after.items() if k not in ("entity_id", "origin")
        }
        assert after["origin"]["note"].startswith(before["origin"]["note"])
        assert "Treasure section" in after["origin"]["note"]
    assert accepted["chapters"][1] == original["chapters"][1]
    assert not await repair_structure(recovery)
    # Rebuild derived acceptance from the exact saved provider response after a crash.
    recovery.store.put("bundles", original)
    with recovery.store.db() as db:
        db.execute("DELETE FROM artifact WHERE name LIKE 'structural-repair-%'")
    assert await repair_structure(recovery)
    assert len(requests) == 1
    assert recovery.store.usage()["call_count"] == 1


@pytest.mark.asyncio
async def test_ai_fictitious_region_references_remain_invalid(recovery):
    original = recovery.store.get("bundles")
    original["chapters"][0]["events"][0]["origin"]["regions"] = ["Fictitious PDF area"]
    recovery.store.put("bundles", original)
    assert not await repair_structure(recovery)
    with pytest.raises(NeedsAttention, match="fictitious PDF evidence"):
        validate_semantics(recovery.store.get("spine"), original, recovery.store.get("source"))
    assert recovery.store.get("bundles") == original
    assert recovery.calls == []


@pytest.mark.asyncio
async def test_partial_reference_result_continues_only_unresolved_rows(recovery):
    original = recovery.store.get("bundles")
    event = original["chapters"][0]["events"][0]
    event["clue_ids"] = ["missing_cargo", "missing_treasure"]
    recovery.store.put("bundles", original)
    requests = []

    async def call(node, prompt, schema, evidence, images, **kwargs):
        assert prompt == "structural-links"
        assert schema is StructuralLinks
        requests.append((node, deepcopy(evidence)))
        identity = "missing_cargo" if len(requests) == 1 else "missing_treasure"
        if len(requests) == 2:
            assert node.startswith("structural-links-residual-")
            assert "chapters" not in evidence
            assert "spine" not in evidence
            assert len(evidence["affected_rows"]) == 1
            assert evidence["allowed_new_entities"] == [
                {
                    "chapter_id": original["chapters"][0]["chapter_id"],
                    "id": identity,
                    "kinds": ["clue"],
                }
            ]
        entity = npc(identity) | {"kind": "clue"}
        return schema.model_validate(
            {
                "new_entities": [
                    {"chapter_id": original["chapters"][0]["chapter_id"], "entity": entity}
                ]
            }
        )

    recovery.semantic = SimpleNamespace(call=call)
    assert await repair_structure(recovery)
    accepted = recovery.store.get("bundles")
    validate_semantics(recovery.store.get("spine"), accepted, recovery.store.get("source"))
    assert accepted["chapters"][0]["events"] == original["chapters"][0]["events"]
    assert len(requests) == 2
    # Replay retained candidates after an aggregate reconstruction; no new payment.
    recovery.store.put("bundles", original)
    assert await repair_structure(recovery)
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_reference_residual_no_progress_is_held_without_resubmission(recovery):
    bundles = recovery.store.get("bundles")
    bundles["chapters"][0]["events"][0]["clue_ids"] = ["missing_cargo"]
    recovery.store.put("bundles", bundles)
    requests = []

    async def call(node, prompt, schema, evidence, images, **kwargs):
        requests.append(node)
        return schema.model_validate({})

    recovery.semantic = SimpleNamespace(call=call)
    with pytest.raises(NeedsAttention, match="made no progress"):
        await repair_structure(recovery)
    assert len(requests) == 2
    with pytest.raises(NeedsAttention, match="made no progress"):
        await repair_structure(recovery)
    assert len(requests) == 2
    assert recovery.store.get("bundles") == bundles


@pytest.mark.asyncio
async def test_v5_rejected_reference_candidate_resumes_without_repaying_original(recovery):
    spine, bundles = recovery.store.get("spine"), recovery.store.get("bundles")
    bundles["chapters"][0]["events"][0]["clue_ids"] = ["missing_cargo"]
    recovery.store.put("bundles", bundles)
    signature = digest({"spine": spine, "bundles": bundles, "issues": []})
    history_key = "structural-repair-" + signature
    recovery.store.put(
        history_key,
        {
            "state": "rejected",
            "policy": "sparse-reference-joins-retain-content-5",
            "reference_candidate": {},
            "error": "Missing cargo identity",
        },
    )
    requests = []

    async def call(node, prompt, schema, evidence, images, **kwargs):
        assert node.startswith("structural-links-residual-")
        requests.append(node)
        return schema.model_validate(
            {
                "new_entities": [
                    {
                        "chapter_id": bundles["chapters"][0]["chapter_id"],
                        "entity": npc("missing_cargo") | {"kind": "clue"},
                    }
                ]
            }
        )

    recovery.semantic = SimpleNamespace(call=call)
    assert await repair_structure(recovery)
    assert len(requests) == 1
    assert recovery.store.get(history_key)["reference_candidate"] == {}
    assert recovery.store.get(history_key)["state"] == "accepted"
