"""Original chapter map pixels survive source partitioning and scoped repairs."""

import base64
import hashlib
from copy import deepcopy
from pathlib import Path

import pytest
from test_campaign_chapter_calls_v2 import (
    ChapterResponses,
    chapter_fixture,
    pdf_store as pdf_store,
    workflow,
)
from test_campaign_chapter_parts_v2 import Responses, evidence_fixture, response

from apps.api.services.campaign_prep import chapter_parts, publication
from apps.api.services.campaign_prep.chapter_parts import ChapterPart, author_chapter
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.source import evidence_pages
from apps.api.services.campaign_prep.store import NeedsAttention
from edgar_core.campaign_semantics import ContinuityPatch, FieldEdit, Patch


def add_distant_map(store):
    source = store.get("source")
    page = deepcopy(source["pages"][0])
    content = b"original-chapter-map-43"
    page.update(
        page=43, image="page-0043.png", text="Original map: verify actual connections.",
        image_sha256=hashlib.sha256(content).hexdigest(),
    )
    (Path(source["directory"]) / page["image"]).write_bytes(content)
    source["pages"].append(page)
    store.put("source", source)
    store.put("visual-candidates", [43])
    return source, content


@pytest.mark.asyncio
async def test_every_part_and_continuation_has_whole_chapter_original_map(pdf_store, monkeypatch):
    evidence, bundle = evidence_fixture(pdf_store)
    source, map_bytes = add_distant_map(pdf_store)
    spine = pdf_store.get("spine")
    spine["chapters"][0]["pages"] = [1, 2, 3, 43]
    spine["chapters"][1]["pages"] = [3]
    pdf_store.put("spine", spine)
    evidence.update(
        requested_chapter=spine["chapters"][0],
        source_context_pages=[1, 2, 3, 43],
        source_evidence=evidence_pages(source),
    )
    monkeypatch.setattr(chapter_parts, "SOURCE_TOKENS_PER_PART", 1)
    sent_images = []

    class Capture(Responses):
        async def create(self, **request):
            sent_images.append([
                base64.b64decode(part["image_url"].split(",", 1)[1])
                for part in request["input"][1]["content"][1:]
            ])
            return await super().create(**request)

    def build(call, index):
        context = call["evidence"]
        assert call["schema"] == "ChapterPart"
        assert sent_images[-1] == [map_bytes]
        assert context["image_pages"] == [43]
        assert context["map_context"][0] == {
            "reference_id": "source_page_0043", "physical_page": 43,
            "source_sha256": source["sha256"],
            "image_sha256": hashlib.sha256(map_bytes).hexdigest(),
            "image": "page-0043.png",
            "role": "Original chapter visual; inspect for relevant map geometry.",
        }
        assert 43 in context["source_context_pages"]
        # The first part owns opening pages, but still receives the far-away map.
        if index <= 2:
            assert 43 not in context["owned_pages"]
        return response(ChapterPart(
            chapter_id=bundle["chapter_id"],
            goals=bundle["goals"] if index == 1 else [],
            events=bundle["events"] if index == 1 else [],
            source_pages=context["owned_pages"], complete=index != 1,
            remaining_work="Finish this location's details." if index == 1 else "",
        ).model_dump_json(), identity=f"resp_map_part_{index}")

    responses = Capture(pdf_store, build)
    result, receipt = await author_chapter(workflow(pdf_store, responses), evidence, [])
    assert result["source_pages"] == [1, 2, 3, 43]
    assert len(responses.requests) == 4  # Three groups; first continues once.
    assert receipt["mode"] == "source-page-parts"
    assert pdf_store.get("source") == source


@pytest.mark.asyncio
async def test_event_local_repair_gets_chapter_map_without_expanding_allowed_records(pdf_store):
    spine, bundles = chapter_fixture()
    source, map_bytes = add_distant_map(pdf_store)
    spine["chapters"][0]["pages"] = [1, 43]
    bundles["chapters"][0]["source_pages"] = [1, 43]
    bundles["chapters"][0]["events"][0]["origin"].update(kind="pdf", pages=[1])
    pdf_store.put("spine", spine)
    pdf_store.put("bundles", bundles)
    pdf_store.put("findings", [{"owner": "critic", "value": {
        "id": "wrong_connection", "severity": "material", "record_ids": ["event_square"],
        "problem": "A route contradicts the map.", "evidence": "Physical map page 43.",
        "repair": "Correct this event's route from the original map.",
    }}])
    calls = []

    class Repair:
        async def call(self, node, prompt, schema, evidence, images):
            calls.append(evidence)
            assert [image.read_bytes() for image in images] == [map_bytes]
            assert evidence["image_pages"] == [43]
            assert evidence["map_context"][0]["reference_id"] == "source_page_0043"
            assert evidence["map_context"][0]["source_sha256"] == source["sha256"]
            assert {p["physical_page"] for p in evidence["source_evidence"]} == {1, 43}
            assert "ch_bell_content" not in evidence["allowed_ids"]
            assert "event_after" not in evidence["allowed_ids"]
            return Patch()

    with pytest.raises(NeedsAttention, match="Repair made no progress"):
        await Workflow(pdf_store, semantic=Repair()).repair({"slug": pdf_store.slug})
    assert len(calls) == 1
    assert pdf_store.get("bundles") == bundles


@pytest.mark.asyncio
async def test_chapter_and_independent_review_include_visual_source_identity(pdf_store, monkeypatch):
    responses = ChapterResponses(pdf_store)
    monkeypatch.setattr(publication, "publish", lambda _: None)
    await workflow(pdf_store, responses).run()
    source = pdf_store.get("source")
    for call in responses.requests:
        if call["schema"] not in {"Bundles", "Review"}:
            continue
        references = call["evidence"]["map_context"]
        assert references
        assert all(row["source_sha256"] == source["sha256"] for row in references)
        assert {row["physical_page"] for row in references} == set(call["evidence"]["image_pages"])


@pytest.mark.asyncio
async def test_identity_resolution_rejects_inherited_narrative_field_edits(pdf_store):
    spine, bundles = chapter_fixture()
    duplicate = deepcopy(spine["entities"][0])
    duplicate["id"] = "duplicate_square"
    spine["entities"].append(duplicate)

    class Identity:
        async def call(self, *_):
            return ContinuityPatch(field_edits=[FieldEdit(
                record_id="event_square", field="dm_notes", old_text="Old", new_text="New",
            )])

    with pytest.raises(NeedsAttention, match="changed unrelated narrative records"):
        await Workflow(pdf_store, semantic=Identity()).resolve_identities(
            spine, bundles, "place_square", "duplicate_square", "The Square"
        )
