"""Fresh graph execution, independent review/repair and cancelled startup recovery."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_campaign_graph_v2 import sample

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep import graph, publication, runtime
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.provider import SemanticClient
from apps.api.services.campaign_prep.store import VERSION, Store, digest
from edgar_core.campaign_semantics import Bundles, Patch, Review, Spine


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    store = Store("fresh_workflow")
    store.initialize(
        {
            "source_kind": "ai",
            "rules_edition": "2014",
            "preferences": "Keep solutions open",
            "generation": {"theme": "A missing keeper and an independent courier", "size": "small"},
        }
    )
    monkeypatch.setattr(graph, "catalogue", lambda _edition: [])
    return store


def two_chapters():
    spine, bundles = deepcopy(sample())
    chapter = deepcopy(spine["chapters"][0])
    chapter.update(
        id="ch_courier",
        title="The courier",
        brief="Help an independent courier",
        source_order=2,
        play_order=2,
        entity_ids=["place_bridge"],
    )
    spine["chapters"].append(chapter)
    place = deepcopy(spine["entities"][0])
    place.update(id="place_bridge", name="The Bridge", description="A riverside bridge")
    spine["entities"].append(place)
    bundle = deepcopy(bundles["chapters"][0])
    bundle["chapter_id"] = "ch_courier"
    bundle["goals"][0].update(
        id="goal_parcel",
        title="The parcel arrives safely",
        event_ids=["event_courier"],
        objective="Help the courier",
        completion="The parcel is delivered through any credible solution",
    )
    bundle["events"][0].update(
        id="event_courier",
        title="The waiting courier",
        location_id="place_bridge",
        goal_ids=["goal_parcel"],
        public_description="A courier waits beside the bridge with a dry parcel.",
        dm_notes="The courier needs help crossing a flooded path. Keep several practical options open.",
    )
    bundles["chapters"].append(bundle)
    bundles["chapters"][0]["events"][0]["dm_notes"] = (
        "A ribbon is present, but its useful clue is missing."
    )
    return Spine.model_validate(spine).model_dump(), Bundles.model_validate(bundles).model_dump()


class LocalResponses:
    """Provider transport fixture: actual durable dispatch/response/parse code remains active."""

    def __init__(self, store, *, unchanged_repair=False):
        self.store = store
        self.spine, self.bundles = two_chapters()
        self.unchanged_repair = unchanged_repair
        self.calls = []
        self.reviews = []
        self.repairs = []

    async def create(self, **request):
        schema_name = request["text"]["format"]["name"]
        evidence = json.loads(request["input"][1]["content"][0]["text"])
        self.calls.append(schema_name)
        dispatch = request["metadata"]["edgar_dispatch_id"]
        assert self.store.task(dispatch)["state"] == "dispatching"
        if schema_name == Spine.__name__:
            value = self.spine
        elif schema_name == Bundles.__name__:
            requested = evidence["requested_chapter"]["id"]
            value = {
                "chapters": [
                    chapter
                    for chapter in self.bundles["chapters"]
                    if chapter["chapter_id"] == requested
                ]
            }
            assert len(value["chapters"]) == 1  # all useful chapter work shares this call
        elif schema_name == Review.__name__:
            self.reviews.append(deepcopy(evidence))
            findings = []
            if len(self.reviews) == 1:
                findings = [
                    {
                        "id": "missing_ribbon_clue",
                        "severity": "material",
                        "record_ids": ["event_square"],
                        "evidence": "The event names a ribbon but gives the DM no useful clue it provides.",
                        "instruction": "Explain what useful lead inspecting the ribbon can earn, with any credible approach.",
                    }
                ]
            value = {"covered_ids": evidence["review_ids"], "findings": findings}
        elif schema_name == Patch.__name__:
            self.repairs.append(deepcopy(evidence))
            event = deepcopy(self.bundles["chapters"][0]["events"][0])
            if not self.unchanged_repair:
                event["dm_notes"] = (
                    "The ribbon belongs to the keeper. Any credible inspection or conversation yields a useful lead to the riverside witness. The hero chooses how to follow it."
                )
            value = Patch(events=[event]).model_dump()
        else:
            raise AssertionError("Unexpected semantic stage " + schema_name)
        payload = {
            "id": "resp_workflow_" + str(len(self.calls)),
            "model": "gpt-6-luna",
            "status": "completed",
            "metadata": request["metadata"],
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}
            ],
        }
        return SimpleNamespace(model_dump=lambda **_: payload)


@pytest.mark.asyncio
async def test_full_fresh_graph_repairs_only_affected_records_and_publishes(
    fresh_store, monkeypatch
):
    responses = LocalResponses(fresh_store)
    semantic = SemanticClient(fresh_store, SimpleNamespace(responses=responses))
    published = []

    def publish(store):
        assert store.get("findings") == []
        assert len(store.get("compiled")["manifest"]["objectives"]) == 2
        published.append(deepcopy(store.get("compiled")))
        store.put("publication", {"compiled_sha256": digest(store.get("compiled"))})

    monkeypatch.setattr(publication, "publish", publish)
    await Workflow(fresh_store, semantic=semantic).run()
    assert fresh_store.state()["status"] == "ready"
    assert len(published) == 1
    assert responses.calls == ["Spine", "Bundles", "Bundles", "Review", "Patch", "Review"]
    assert set(responses.reviews[1]["review_ids"]) == {"event_square", "goal_keeper"}
    assert "event_courier" in responses.reviews[0]["review_ids"]
    assert "event_courier" not in responses.reviews[1]["review_ids"]
    assert set(responses.repairs[0]["allowed_ids"]) == {"event_square", "goal_keeper"}
    assert fresh_store.get("bundles")["chapters"][1] == responses.bundles["chapters"][1]
    assert "useful lead" in fresh_store.get("bundles")["chapters"][0]["events"][0]["dm_notes"]
    assert len(fresh_store.get("repair-history")) == 1
    with fresh_store.db() as db:
        retained = [dict(row) for row in db.execute("SELECT * FROM task")]
    assert len(retained) == 6 and all(row["accepted"] for row in retained)
    assert all(json.loads(row["response"])["status"] == "completed" for row in retained)
    # The initial independent defect report remains in its immutable paid response receipt.
    assert any("missing_ribbon_clue" in row["response"] for row in retained)
    reopened = Store(fresh_store.slug)
    assert reopened.get("publication") == fresh_store.get("publication")
    assert reopened.usage()["call_count"] == 6


@pytest.mark.asyncio
async def test_unchanged_repair_holds_findings_and_paid_receipts_without_loop(
    fresh_store, monkeypatch
):
    responses = LocalResponses(fresh_store, unchanged_repair=True)

    def no_publication(_store):
        raise AssertionError("Unresolved material findings must prevent publication")

    monkeypatch.setattr(publication, "publish", no_publication)
    semantic = SemanticClient(fresh_store, SimpleNamespace(responses=responses))
    monkeypatch.setattr(graph, "Workflow", lambda store: Workflow(store, semantic=semantic))
    adventures_meta.save_meta(
        fresh_store.slug,
        {
            "preparation_workflow": VERSION,
            "private_preparation": True,
            "rules_edition": "2014",
            "job": {"job_id": "1" * 32, "kind": "generation"},
        },
    )
    await runtime.prepare_adventure(fresh_store.slug)
    assert responses.calls == ["Spine", "Bundles", "Bundles", "Review", "Patch"]
    assert fresh_store.state()["status"] == "blocked"
    assert fresh_store.state()["error_type"] == "NeedsAttention"
    assert "made no progress" in fresh_store.state()["error"]
    assert fresh_store.get("findings")[0]["value"]["id"] == "missing_ribbon_clue"
    assert fresh_store.get("publication") is None
    assert fresh_store.usage()["call_count"] == 5
    with fresh_store.db() as db:
        assert db.execute("SELECT count(*) FROM task WHERE accepted IS NOT NULL").fetchone()[0] == 5
    assert Store(fresh_store.slug).get("findings") == fresh_store.get("findings")


@pytest.mark.asyncio
async def test_startup_recovers_cancelled_receipts_without_dispatching_preparation(
    fresh_store, monkeypatch
):
    fresh_store.update(status="cancelled", cancel_requested=True)
    adventures_meta.save_meta(
        fresh_store.slug,
        {"preparation_workflow": VERSION, "private_preparation": True, "status": "failed"},
    )
    reconciled = []

    async def recover_cancellation(self):
        reconciled.append(self.store.slug)
        return False

    async def no_preparation(*_, **__):
        raise AssertionError("A cancelled run must not dispatch campaign preparation")

    monkeypatch.setattr(SemanticClient, "reconcile_cancellation", recover_cancellation)
    monkeypatch.setattr(runtime, "prepare_adventure", no_preparation)
    await runtime.recover_private_preparations()
    tasks = tuple(runtime._TASKS)
    assert len(tasks) == 1 and tasks[0].get_name().startswith("cancel-campaign-v2-")
    await asyncio.gather(*tasks)
    assert reconciled == [fresh_store.slug]
    assert fresh_store.state()["cancellation_reconciled"] is True
    assert fresh_store.state()["status"] == "cancelled"
    assert fresh_store.usage()["call_count"] == 0
