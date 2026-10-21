"""Durable incremental chapter recovery with real request/receipt construction."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_campaign_chapter_calls_v2 import chapter_fixture, pdf_store as pdf_store, workflow

from apps.api.services.campaign_prep import chapter_parts
from apps.api.services.campaign_prep.chapter_parts import (
    ChapterPart,
    author_chapter,
    blank,
    closed_content,
    merge,
    page_groups,
)
from apps.api.services.campaign_prep.provider import OutputExhausted
from apps.api.services.campaign_prep.source import evidence_pages
from apps.api.services.campaign_prep.store import NeedsAttention, Store
from edgar_core.campaign_semantics import Entity


def evidence_fixture(store):
    spine, bundles = chapter_fixture()
    store.put("spine", spine)
    store.put("visual-candidates", [])
    chapter = spine["chapters"][0]
    evidence = {
        "campaign": {
            key: spine[key]
            for key in ("title", "premise", "setting", "hook", "ending", "dm_guidance")
        },
        "all_chapter_briefs": spine["chapters"],
        "shared_entities": spine["entities"],
        "shared_truths": [{"id": "future_truth", "truth": "The keeper knows the key's old owner."}],
        "requested_chapter": chapter,
        "source_evidence": evidence_pages(store.get("source"), chapter["pages"]),
        "source_context_pages": chapter["pages"],
        "source_kind": "pdf",
        "original_request": None,
        "accepted_dependencies": [],
        "verified_creatures": [],
        "preferences": "Keep every source detail.",
        "image_pages": [],
    }
    return evidence, bundles["chapters"][0]


def response(text, *, incomplete=False, identity="resp_partial"):
    return {
        "id": identity,
        "model": "gpt-6-luna",
        "status": "incomplete" if incomplete else "completed",
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
        "usage": {"input_tokens": 100, "output_tokens": 128_000 if incomplete else 100},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


class Responses:
    def __init__(self, store, build):
        self.store, self.build = store, build
        self.requests = []

    async def create(self, **request):
        task = self.store.task(request["metadata"]["edgar_dispatch_id"])
        assert task["state"] == "dispatching"
        assert request["model"] == "gpt-6-luna"
        assert request["reasoning"] == {"effort": "medium"}
        evidence = json.loads(request["input"][1]["content"][0]["text"])
        self.requests.append({"schema": request["text"]["format"]["name"], "evidence": evidence})
        value = self.build(self.requests[-1], len(self.requests))
        return SimpleNamespace(model_dump=lambda **_: value)


def partial_original(bundle):
    return (
        '{"chapters":[{"chapter_id":'
        + json.dumps(bundle["chapter_id"])
        + ',"goals":'
        + json.dumps(bundle["goals"])
        + ',"events":'
        + json.dumps(bundle["events"])
        + ',"new_entities":[{"id":"npc_keeper","kind":"npc","name":"The Keeper",'
        '"aliases":[],"description":"A worried keeper.","motive":"Keep the bridge safe.",'
        '"knowledge":"The old key is hidden.","origin":{"kind":"pdf","pages":[1],'
        '"regions":[],"note":"source' + " " * 3000
    )


def keeper():
    return Entity.model_validate(
        {
            "id": "npc_keeper",
            "kind": "npc",
            "name": "The Keeper",
            "aliases": [],
            "description": "A worried keeper.",
            "motive": "Keep the bridge safe.",
            "knowledge": "The old key is hidden.",
            "origin": {
                "kind": "pdf",
                "pages": [1],
                "regions": [],
                "note": "source page 1",
                "uncertain": False,
            },
        }
    ).model_dump()


@pytest.mark.asyncio
async def test_original_incomplete_replays_without_resubmit_and_distinct_completion_survives_restart(
    pdf_store,
):
    evidence, bundle = evidence_fixture(pdf_store)
    original_text = partial_original(bundle)

    def build(call, index):
        if call["schema"] == "Bundles":
            assert index == 1
            return response(original_text, incomplete=True)
        assert call["schema"] == "ChapterPart"
        retained = call["evidence"]["retained_chapter"]
        assert retained["goals"] == bundle["goals"]
        assert retained["events"] == bundle["events"]
        assert retained["new_entities"] == []
        part = ChapterPart(
            chapter_id=bundle["chapter_id"],
            new_entities=[keeper()],
            dm_guidance=bundle["dm_guidance"],
            source_notes=bundle["source_notes"],
            source_pages=call["evidence"]["owned_pages"],
            complete=True,
        )
        return response(part.model_dump_json(), identity="resp_completion")

    responses = Responses(pdf_store, build)
    result, receipt = await author_chapter(workflow(pdf_store, responses), evidence, [])
    assert result["goals"] == bundle["goals"] and result["events"] == bundle["events"]
    assert result["new_entities"] == [keeper()]
    assert result["source_pages"] == bundle["source_pages"]
    assert receipt["mode"] == "output-recovery"
    assert [request["schema"] for request in responses.requests] == ["Bundles", "ChapterPart"]
    with pdf_store.db() as db:
        original = dict(
            db.execute(
                "SELECT * FROM task WHERE node=?", ("chapter-" + bundle["chapter_id"],)
            ).fetchone()
        )
    assert original["accepted"] is None and original["state"] == "incomplete"
    assert json.loads(original["response"])["output"][0]["content"][0]["text"] == original_text
    projection = pdf_store.get("chapter-recovered-" + original["id"])
    assert projection["parent_task_id"] == original["id"]
    assert projection["provisional_records"]["new_entities"] == []
    reopened = Store(pdf_store.slug)
    replayed, replay_receipt = await author_chapter(workflow(reopened, responses), evidence, [])
    assert replayed == result and replay_receipt == receipt
    assert len(responses.requests) == 2 and reopened.usage()["call_count"] == 2
    assert reopened.task(original["id"]) == original


def test_unfinished_origin_is_not_completed_or_fabricated():
    _, bundles = chapter_fixture()
    bundle = bundles["chapters"][0]
    exhausted = OutputExhausted(
        "original-task", response(partial_original(bundle), incomplete=True)
    )
    retained, pending = closed_content(exhausted, bundle["chapter_id"], wrapped=True)
    assert retained["new_entities"] == []
    assert retained["goals"] == bundle["goals"]
    assert any(
        row["path"] == ["chapters", 0, "new_entities", 0, "origin", "kind"] for row in pending
    )
    assert not any(row["path"] == ["chapters", 0, "new_entities", 0, "origin"] for row in pending)
    assert not any(row["path"][-1] == "note" for row in pending)


@pytest.mark.asyncio
async def test_closed_chapter_missing_outer_syntax_recovers_without_completion_call(pdf_store):
    evidence, bundle = evidence_fixture(pdf_store)
    text = '{"chapters":[' + json.dumps(bundle)

    def build(call, index):
        assert index == 1 and call["schema"] == "Bundles"
        return response(text, incomplete=True)

    responses = Responses(pdf_store, build)
    result, receipt = await author_chapter(workflow(pdf_store, responses), evidence, [])
    assert result == bundle and receipt["mode"] == "closed-output-recovery"
    replayed, _ = await author_chapter(workflow(Store(pdf_store.slug), responses), evidence, [])
    assert replayed == result and len(responses.requests) == 1
    with pdf_store.db() as db:
        row = db.execute("SELECT state,accepted FROM task").fetchone()
    assert row["state"] == "incomplete" and row["accepted"] is None


@pytest.mark.asyncio
async def test_dense_chapter_owns_disjoint_pages_and_supplies_original_context_and_truths(
    pdf_store, monkeypatch
):
    evidence, bundle = evidence_fixture(pdf_store)
    source = pdf_store.get("source")
    for number in (4, 5):
        page = deepcopy(source["pages"][0])
        page.update(page=number, text=f"Exact original physical page {number}: a distinct room.")
        source["pages"].append(page)
    pdf_store.put("source", source)
    spine = pdf_store.get("spine")
    spine["chapters"][0]["pages"] = [1, 2, 3, 4, 5]
    spine["chapters"][1]["pages"] = [5]
    pdf_store.put("spine", spine)
    evidence.update(
        requested_chapter=spine["chapters"][0],
        all_chapter_briefs=spine["chapters"],
        source_context_pages=[1, 2, 3, 4, 5],
        source_evidence=evidence_pages(source),
    )
    monkeypatch.setattr(chapter_parts, "SOURCE_TOKENS_PER_PART", 1)
    groups = page_groups(spine, evidence["requested_chapter"], source)
    assert len(groups) == 4
    owned = [page for group in groups for page in group]
    assert sorted(owned) == [1, 2, 3, 4, 5] and len(owned) == len(set(owned))

    def build(call, index):
        assert call["schema"] == "ChapterPart"
        context = call["evidence"]
        assert context["shared_truths"] == evidence["shared_truths"]
        assert context["shared_entities"] == evidence["shared_entities"]
        assert 5 in context["source_context_pages"]  # Shared appendix is reread each time.
        for page in context["source_evidence"]:
            assert page["text"] == next(
                row["text"] for row in source["pages"] if row["page"] == page["physical_page"]
            )
        part = ChapterPart(
            chapter_id=bundle["chapter_id"],
            goals=bundle["goals"] if index == 1 else [],
            events=bundle["events"] if index == 1 else [],
            dm_guidance=bundle["dm_guidance"] if index == 1 else "",
            source_notes="Useful source details for pages " + str(context["owned_pages"]),
            source_pages=context["owned_pages"],
            complete=True,
        )
        return response(part.model_dump_json(), identity=f"resp_part_{index}")

    responses = Responses(pdf_store, build)
    result, receipt = await author_chapter(workflow(pdf_store, responses), evidence, [])
    assert result["source_pages"] == [1, 2, 3, 4, 5]
    assert len(result["goals"]) == len(result["events"]) == 1
    assert receipt["mode"] == "source-page-parts" and len(receipt["parts"]) == 4
    assert [item["owned_pages"] for item in receipt["parts"]] == groups
    replayed, _ = await author_chapter(workflow(Store(pdf_store.slug), responses), evidence, [])
    assert replayed == result and len(responses.requests) == 4


def test_conflicting_duplicate_record_is_rejected_without_overwrite():
    _, bundles = chapter_fixture()
    goal = bundles["chapters"][0]["goals"][0]
    retained = merge(blank("ch_bell"), {"goals": [goal]})
    assert merge(retained, {"goals": [deepcopy(goal)]}) == retained
    changed = {**goal, "completion": "A conflicting prescribed outcome."}
    with pytest.raises(NeedsAttention, match="conflict on retained record"):
        merge(retained, {"goals": [changed]})
    assert retained["goals"] == [goal]


@pytest.mark.asyncio
async def test_exhausted_closed_part_needs_no_extra_completion(pdf_store, monkeypatch):
    evidence, bundle = evidence_fixture(pdf_store)
    spine = pdf_store.get("spine")
    spine["chapters"][0]["pages"] = [1, 2, 3]
    spine["chapters"][1]["pages"] = [3]
    pdf_store.put("spine", spine)
    evidence.update(
        requested_chapter=spine["chapters"][0],
        source_context_pages=[1, 2, 3],
        source_evidence=evidence_pages(pdf_store.get("source")),
    )
    monkeypatch.setattr(chapter_parts, "SOURCE_TOKENS_PER_PART", 1)

    def build(call, index):
        assert call["schema"] == "ChapterPart"
        part = ChapterPart(
            chapter_id=bundle["chapter_id"],
            goals=bundle["goals"] if index == 1 else [],
            events=bundle["events"] if index == 1 else [],
            dm_guidance=bundle["dm_guidance"] if index == 1 else "",
            source_pages=call["evidence"]["owned_pages"],
            complete=True,
        )
        return response(part.model_dump_json(), incomplete=True, identity=f"resp_closed_{index}")

    responses = Responses(pdf_store, build)
    result, _ = await author_chapter(workflow(pdf_store, responses), evidence, [])
    assert result["goals"] == bundle["goals"]
    assert len(responses.requests) == 2
    with pdf_store.db() as db:
        assert {r["state"] for r in db.execute("SELECT state FROM task")} == {"incomplete"}
    replayed, _ = await author_chapter(workflow(Store(pdf_store.slug), responses), evidence, [])
    assert replayed == result and len(responses.requests) == 2


@pytest.mark.asyncio
async def test_closed_exhausted_multiple_chapters_is_rejected(pdf_store):
    evidence, bundle = evidence_fixture(pdf_store)

    def build(call, index):
        return response(json.dumps({"chapters": [bundle, bundle]}), incomplete=True)

    responses = Responses(pdf_store, build)
    with pytest.raises(NeedsAttention, match="exactly one chapter"):
        await author_chapter(workflow(pdf_store, responses), evidence, [])
    assert len(responses.requests) == 1
