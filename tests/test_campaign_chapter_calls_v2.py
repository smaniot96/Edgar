"""The ordinary PDF path keeps original evidence in one authoring call per chapter."""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_campaign_graph_v2 import sample

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep import graph, publication
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.provider import SemanticClient
from apps.api.services.campaign_prep.store import NeedsAttention, Store
from edgar_core.campaign_semantics import Bundles, Patch, Spine


def source_fixture(directory):
    pages = []
    for number, text in (
        (1, "Opening: the keeper requests help. " * 80 + "Exact opening tail: a blue ribbon."),
        (
            2,
            "Compound: three guards watch the bridge. " * 80
            + "Exact compound tail: forty feet high.",
        ),
        (
            3,
            "Shared appendix: three charges; recover one at dawn. " * 80
            + "Exact appendix tail: DC 17 Arcana.",
        ),
    ):
        image = f"page-{number:04d}.png"
        # The provider transports original bytes; these are deliberately distinguishable.
        content = b"source-image-" + str(number).encode()
        (directory / image).write_bytes(content)
        pages.append(
            {
                "page": number,
                "text": text,
                "image": image,
                "image_sha256": hashlib.sha256(content).hexdigest(),
                "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "width": 1000,
                "height": 1400,
                "regions": [{"id": f"p{number}-b1", "bbox": [1, 2, 80, 90]}],
                "quality": {"mean_confidence": 93, "visual_interpretation_needed": number > 1},
            }
        )
    return {"kind": "pdf", "sha256": "a" * 64, "directory": str(directory), "pages": pages}


def chapter_fixture():
    spine, bundles = deepcopy(sample())
    spine["chapters"][0]["pages"] = [1, 3]
    spine["important_visual_pages"] = [1]  # selected by planning, beyond automatic candidates
    first = bundles["chapters"][0]
    first["source_pages"] = [1, 3]
    first["source_notes"] = "The shared appendix applies at the opening and afterward."
    chapter = deepcopy(spine["chapters"][0])
    chapter.update(
        id="ch_after",
        title="The compound",
        brief="Help the keeper at the compound.",
        source_order=2,
        play_order=2,
        pages=[2, 3],
        depends_on=["ch_bell"],
    )
    spine["chapters"].append(chapter)
    second = deepcopy(first)
    second.update(chapter_id="ch_after", source_pages=[2, 3])
    second["events"][0].update(id="event_after", goal_ids=["goal_after"])
    second["goals"][0].update(id="goal_after", event_ids=["event_after"])
    bundles["chapters"].append(second)
    return Spine.model_validate(spine).model_dump(), Bundles.model_validate(bundles).model_dump()


@pytest.fixture
def pdf_store(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    store = Store("chapter_context")
    store.initialize(
        {"source_kind": "pdf", "rules_edition": "2014", "preferences": "Keep every source detail."}
    )
    images = tmp_path / "source"
    images.mkdir()
    store.put("source", source_fixture(images))
    store.put("mechanics", [])
    monkeypatch.setattr(graph, "candidate_visual_pages", lambda _: [2, 3])
    monkeypatch.setattr(graph, "visual_page_reasons", lambda *_: ["source table or map"])
    return store


class ChapterResponses:
    """Keep durable dispatch, actual request construction and schema acceptance active."""

    def __init__(self, store, *, spine=None, bad_inventory=None):
        self.store = store
        self.spine, self.bundles = chapter_fixture()
        if spine is not None:
            self.spine = spine
        self.bad_inventory = bad_inventory
        self.requests = []

    async def create(self, **request):
        dispatch = request["metadata"]["edgar_dispatch_id"]
        assert self.store.task(dispatch)["state"] == "dispatching"
        assert request["model"] == "gpt-6-luna"
        assert request["reasoning"] == {"effort": "medium"}
        schema = request["text"]["format"]["name"]
        parts = request["input"][1]["content"]
        evidence = json.loads(parts[0]["text"])
        images = [base64.b64decode(part["image_url"].split(",", 1)[1]) for part in parts[1:]]
        self.requests.append({"schema": schema, "evidence": evidence, "images": images})
        if schema == "Spine":
            value = self.spine
        elif schema == "Bundles":
            requested = evidence["requested_chapter"]
            chapter = deepcopy(
                next(b for b in self.bundles["chapters"] if b["chapter_id"] == requested["id"])
            )
            if self.bad_inventory is not None:
                chapter["source_pages"] = self.bad_inventory
            value = {"chapters": [chapter]}
        elif schema == "Review":
            value = {"covered_ids": evidence["review_ids"], "findings": []}
        else:
            raise AssertionError("Unexpected extra semantic stage " + schema)
        response = {
            "id": "resp_chapter_" + str(len(self.requests)),
            "model": "gpt-6-luna",
            "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}
            ],
        }
        return SimpleNamespace(model_dump=lambda **_: response)


def workflow(store, responses):
    semantic = SemanticClient(store, SimpleNamespace(responses=responses))
    return Workflow(store, semantic=semantic)


@pytest.mark.asyncio
async def test_one_plan_one_call_per_chapter_and_one_final_review_with_original_evidence(
    pdf_store, monkeypatch
):
    responses = ChapterResponses(pdf_store)
    published = []

    def publish(store):
        published.append(deepcopy(store.get("compiled")))
        store.put("publication", {"ok": True})

    monkeypatch.setattr(publication, "publish", publish)
    await workflow(pdf_store, responses).run()
    assert pdf_store.state()["status"] == "ready"
    assert [r["schema"] for r in responses.requests] == ["Spine", "Bundles", "Bundles", "Review"]
    assert len(published) == 1
    source = pdf_store.get("source")
    source_text = {p["page"]: p["text"] for p in source["pages"]}
    planning, first, second, review = responses.requests
    assert {
        p["physical_page"]: p["text"] for p in planning["evidence"]["source_pages"]
    } == source_text
    assert planning["images"] == [b"source-image-2", b"source-image-3"]
    for call, expected in ((first, [1, 3]), (second, [2, 3])):
        evidence = call["evidence"]
        assert {p["physical_page"]: p["text"] for p in evidence["source_evidence"]} == {
            p: source_text[p] for p in expected
        }
        assert evidence["shared_entities"] == responses.spine["entities"]
        assert evidence["shared_truths"] == responses.spine["secrets"]
        assert len(evidence["all_chapter_briefs"]) == 2
        assert "source_readings" not in evidence
    assert first["images"] == [b"source-image-1", b"source-image-3"]
    assert second["images"] == [b"source-image-2", b"source-image-3"]
    assert first["evidence"]["accepted_dependencies"] == []
    dossier = second["evidence"]["accepted_dependencies"][0]
    assert dossier["chapter_id"] == "ch_bell"
    assert (
        dossier["goals"][0]["completion"]
        == responses.bundles["chapters"][0]["goals"][0]["completion"]
    )
    assert dossier["event_connections"][0]["id"] == "event_square"
    assert dossier["new_entities"] == [] and dossier["new_secrets"] == []
    # Chapter prose and original source stay out of dependency context; that evidence is
    # passed directly for the chapter to which it belongs.
    assert "source_notes" not in dossier and "events" not in dossier
    assert "dm_notes" not in dossier["event_connections"][0]
    assert {
        p["physical_page"]: p["text"] for p in review["evidence"]["source_evidence"]
    } == source_text
    assert review["images"] == [b"source-image-1", b"source-image-2", b"source-image-3"]
    context = pdf_store.get("chapter-context-ch_after")
    assert [p["page"] for p in context["source_pages"]] == [2, 3]
    assert context["source_pages"][1]["raw_sha256"] == source["pages"][2]["raw_sha256"]
    assert pdf_store.usage()["call_count"] == 4

    # Lose the final aggregation and one derived chapter artifact after its paid response.
    # Reconstruct them from the durable provider receipt without another create request.
    with pdf_store.db() as db:
        db.execute(
            "DELETE FROM artifact WHERE name IN (?, ?, ?)",
            ("bundles", "chapter-ch_after", "chapter-context-ch_after"),
        )
    reopened = Store(pdf_store.slug)
    resumed = workflow(reopened, responses)
    await resumed.source({"slug": reopened.slug})
    assert reopened.get("visual-candidates") == [2, 3]
    await resumed.spine({"slug": reopened.slug})
    assert reopened.get("visual-candidates") == [1, 2, 3]
    await resumed.bundles({"slug": reopened.slug})
    assert reopened.get("bundles") == responses.bundles
    assert reopened.get("chapter-context-ch_after") == context
    assert len(responses.requests) == 4

    # Reopen and replay the complete graph using its persisted artifacts/receipts.
    await workflow(reopened, responses).run()
    assert len(responses.requests) == 4
    assert reopened.usage()["call_count"] == 4
    assert reopened.get("source") == source
    with reopened.db() as db:
        assert db.execute("SELECT count(*) FROM task WHERE accepted IS NOT NULL").fetchone()[0] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assignment,match", [([1], "every physical page"), ([1, 3, 9], "nonexistent physical pages")]
)
async def test_planning_must_cover_real_physical_pages(pdf_store, assignment, match):
    spine, _ = chapter_fixture()
    spine["chapters"][0]["pages"] = assignment
    # Leave page 2 assigned but make appendix coverage depend on the first chapter.
    spine["chapters"][1]["pages"] = [2]
    responses = ChapterResponses(pdf_store, spine=spine)
    active = workflow(pdf_store, responses)
    await active.source({"slug": pdf_store.slug})
    with pytest.raises(NeedsAttention, match=match):
        await active.spine({"slug": pdf_store.slug})
    assert pdf_store.get("spine") is None
    assert len(responses.requests) == 1
    # Paid output remains accepted for an explicit repair; it is not submitted twice.
    with pytest.raises(NeedsAttention, match=match):
        await workflow(Store(pdf_store.slug), responses).spine({"slug": pdf_store.slug})
    assert len(responses.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("inventory", [[1, 3, 9]])
async def test_chapter_must_acknowledge_exact_assigned_pages_without_paid_resubmit(
    pdf_store, inventory
):
    spine, _ = chapter_fixture()
    pdf_store.put("spine", spine)
    responses = ChapterResponses(pdf_store, bad_inventory=inventory)
    active = workflow(pdf_store, responses)
    await active.source({"slug": pdf_store.slug})
    with pytest.raises(NeedsAttention, match="outside its supplied evidence"):
        await active.bundles({"slug": pdf_store.slug})
    assert pdf_store.get("bundles") is None
    assert pdf_store.get("chapter-ch_bell") is None
    assert len(responses.requests) == 1
    with pytest.raises(NeedsAttention, match="outside its supplied evidence"):
        await workflow(Store(pdf_store.slug), responses).bundles({"slug": pdf_store.slug})
    assert len(responses.requests) == 1


@pytest.mark.asyncio
async def test_page_ownership_is_local_and_does_not_regenerate_paid_content(pdf_store):
    spine, bundles = chapter_fixture()
    pdf_store.put("spine", spine)
    responses = ChapterResponses(pdf_store, bad_inventory=[])
    active = workflow(pdf_store, responses)
    await active.source({"slug": pdf_store.slug})
    await active.bundles({"slug": pdf_store.slug})
    assert pdf_store.get("bundles") == bundles
    assert len(responses.requests) == 2
    for chapter in spine["chapters"]:
        context = pdf_store.get("chapter-context-" + chapter["id"])
        assert context["model_source_pages"] == []
        assert context["model_omitted_inventory_pages"] == chapter["pages"]
        assert [row["page"] for row in context["source_pages"]] == chapter["pages"]
    await workflow(Store(pdf_store.slug), responses).bundles({"slug": pdf_store.slug})
    assert len(responses.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_unchanged", [False, True])
async def test_chapter_repair_preserves_unaffected_contained_entity(pdf_store, omit_unchanged):
    spine, bundles = chapter_fixture()
    observer = {
        "id": "npc_observer",
        "kind": "npc",
        "name": "Independent Observer",
        "description": "An independent witness whose accepted motive is unchanged.",
        "motive": "Keep the keeper safe.",
        "knowledge": "",
        "aliases": [],
        "origin": {"kind": "authored", "pages": [], "regions": [], "note": "", "uncertain": False},
    }
    bundles["chapters"][0]["new_entities"].append(observer)
    pdf_store.put("spine", spine)
    pdf_store.put("bundles", bundles)
    pdf_store.put(
        "findings",
        [
            {
                "owner": "test",
                "value": {
                    "id": "appendix_detail",
                    "severity": "material",
                    "record_ids": ["ch_bell_content"],
                    "evidence": "The chapter reference has omitted a source detail.",
                    "instruction": "Repair only its detailed running reference.",
                },
            }
        ],
    )
    original = deepcopy(pdf_store.get("bundles"))

    class Repair:
        async def call(self, node, prompt, schema, evidence, images=None):
            assert prompt == "repair" and schema is Patch
            assert "ch_bell_content" in evidence["allowed_ids"]
            assert "npc_observer" not in evidence["allowed_ids"]
            replacement = deepcopy(bundles["chapters"][0])
            replacement["source_notes"] += " The omitted appendix detail is restored."
            if omit_unchanged:
                replacement["new_entities"] = []
            else:
                replacement["new_entities"][0]["motive"] = "A conflicting invented motive."
            return Patch(chapters=[replacement])

    if omit_unchanged:
        await Workflow(pdf_store, semantic=Repair()).repair({"slug": pdf_store.slug})
        repaired = pdf_store.get("bundles")["chapters"][0]
        assert repaired["new_entities"] == original["chapters"][0]["new_entities"]
        assert repaired["source_notes"].endswith("The omitted appendix detail is restored.")
        assert pdf_store.get("repair-history")
    else:
        with pytest.raises(NeedsAttention, match="unrelated contained records"):
            await Workflow(pdf_store, semantic=Repair()).repair({"slug": pdf_store.slug})
        assert pdf_store.get("bundles") == original
        assert pdf_store.get("repair-history") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("include_supporting_inventory", [False, True])
async def test_shared_origins_restore_appendix_and_uncertain_image_without_extra_calls(
    pdf_store, monkeypatch, include_supporting_inventory
):
    responses = ChapterResponses(pdf_store)
    responses.spine["chapters"][0]["pages"] = [1]
    model_pages = [1, 3] if include_supporting_inventory else [1]
    responses.bundles["chapters"][0]["source_pages"] = model_pages
    responses.spine["important_visual_pages"] = []
    responses.spine["entities"][0]["origin"].update(kind="pdf", pages=[3], uncertain=True)
    monkeypatch.setattr(graph, "candidate_visual_pages", lambda _: [])
    monkeypatch.setattr(publication, "publish", lambda _: None)
    await workflow(pdf_store, responses).run()
    assert [r["schema"] for r in responses.requests] == ["Spine", "Bundles", "Bundles", "Review"]
    first = responses.requests[1]
    assert first["evidence"]["requested_chapter"]["pages"] == [1]
    assert first["evidence"]["source_context_pages"] == [1, 3]
    assert [p["physical_page"] for p in first["evidence"]["source_evidence"]] == [1, 3]
    assert first["images"] == [b"source-image-3"]
    assert [p["page"] for p in pdf_store.get("chapter-context-ch_bell")["source_pages"]] == [1, 3]
    assert pdf_store.get("chapter-ch_bell")["source_pages"] == [1]
    assert pdf_store.get("chapter-context-ch_bell")["model_source_pages"] == model_pages
    with pdf_store.db() as db:
        paid = db.execute("SELECT accepted FROM task WHERE node='chapter-ch_bell'").fetchone()
    assert json.loads(paid[0])["chapters"][0]["source_pages"] == model_pages
    compiled = pdf_store.get("compiled")
    scene = next(s for s in compiled["manifest"]["scenes"] if s["scene_id"] == "event_square")
    assert "source_page_0003" in scene["references"]


def test_verified_profile_context_uses_complete_names_and_source_plurals(pdf_store):
    names = ["Bat", "Cat", "Imp", "Mage", "Ape", "Pony", "Wolf", "Brown Bear"]
    pdf_store.put("mechanics", [{"name": name, "rules_edition": "2014"} for name in names])
    evidence = [{"text": "A complicated battle: three wolves, brown bears, apes and ponies."}]
    result = Workflow(pdf_store, semantic=object()).templates(evidence)
    assert [r["name"] for r in result["relevant_source_profiles"]] == [
        "Ape",
        "Pony",
        "Wolf",
        "Brown Bear",
    ]
    assert len(result["index"]) == len(names)


@pytest.mark.asyncio
async def test_chapter_content_patch_retains_omitted_inventory_and_separate_event_edit(pdf_store):
    spine, bundles = chapter_fixture()
    chapter = bundles["chapters"][0]
    extra = deepcopy(chapter["events"][0])
    extra.update(
        id="event_unchanged", title="Unchanged source scene", dm_notes="Exact accepted prose."
    )
    chapter["events"].append(extra)
    extra_goal = deepcopy(chapter["goals"][0])
    extra_goal.update(id="goal_unchanged", title="Unchanged goal", event_ids=[extra["id"]])
    chapter["goals"].append(extra_goal)
    pdf_store.put("spine", spine)
    pdf_store.put("bundles", bundles)
    pdf_store.put(
        "findings",
        [
            {
                "owner": "test",
                "value": {
                    "id": "chapter_detail",
                    "severity": "material",
                    "record_ids": ["ch_bell_content"],
                    "evidence": "A chapter running note and its scene need a source detail.",
                    "instruction": "Restore that detail without changing other records.",
                },
            }
        ],
    )
    original = deepcopy(bundles)

    class Repair:
        async def call(self, node, prompt, schema, evidence, images=None):
            partial = deepcopy(chapter)
            partial["events"] = partial["events"][:1]
            partial["goals"] = partial["goals"][:1]
            partial["source_notes"] += " Restored source detail."
            individual = deepcopy(chapter["events"][0])
            individual["dm_notes"] += " Independently permitted scene detail."
            return Patch(chapters=[partial], events=[individual])

    await Workflow(pdf_store, semantic=Repair()).repair({"slug": pdf_store.slug})
    repaired = pdf_store.get("bundles")
    for field in ("events", "goals", "new_entities", "new_secrets"):
        assert [r["id"] for r in repaired["chapters"][0][field]] == [
            r["id"] for r in original["chapters"][0][field]
        ]
    assert repaired["chapters"][0]["events"][1] == original["chapters"][0]["events"][1]
    assert repaired["chapters"][0]["goals"] == original["chapters"][0]["goals"]
    assert repaired["chapters"][1] == original["chapters"][1]
    assert repaired["chapters"][0]["events"][0]["dm_notes"].endswith(
        "Independently permitted scene detail."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["events", "goals"])
@pytest.mark.parametrize("change", ["invent", "rename", "duplicate"])
async def test_partial_chapter_patch_rejects_new_renamed_or_duplicate_identities(
    pdf_store, field, change
):
    spine, bundles = chapter_fixture()
    pdf_store.put("spine", spine)
    pdf_store.put("bundles", bundles)
    pdf_store.put(
        "findings",
        [
            {
                "owner": "test",
                "value": {
                    "id": "chapter_detail",
                    "severity": "material",
                    "record_ids": ["ch_bell_content"],
                    "evidence": "An omitted chapter detail.",
                    "instruction": "Restore the detail.",
                },
            }
        ],
    )

    class Repair:
        async def call(self, node, prompt, schema, evidence, images=None):
            partial = deepcopy(bundles["chapters"][0])
            proposed = deepcopy(partial[field][0])
            if change != "duplicate":
                proposed["id"] = "invented_identity"
            if change == "rename":
                partial[field][0] = proposed
            else:
                partial[field].append(proposed)
            return Patch(chapters=[partial])

    with pytest.raises(NeedsAttention, match="contained record inventory"):
        await Workflow(pdf_store, semantic=Repair()).repair({"slug": pdf_store.slug})
    assert pdf_store.get("bundles") == bundles


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_page", "review_provenance"),
    [(None, False), (2, False), (99, False), (2, True), (99, True)],
)
async def test_incomplete_chapter_correction_retains_reference_guidance_and_procedures(
    pdf_store, extra_page, review_provenance
):
    spine, bundles = chapter_fixture()
    chapter = bundles["chapters"][0]
    clue = deepcopy(spine["entities"][0])
    clue.update(id="clue_relic", kind="clue", name="Relic")
    chapter["new_entities"] = [clue]
    origin = deepcopy(clue["origin"])
    chapter["items"] = [
        {
            "entity_id": clue["id"],
            "procedure": "Exact accepted item operating procedure.",
            "event_ids": ["event_square"],
            "portable": True,
            "consumable": False,
            "origin": origin,
        }
    ]
    chapter["checks"] = [
        {
            "id": "inspect_relic",
            "action": "Inspect the relic",
            "event_ids": ["event_square"],
            "check": "arcana",
            "dc": 15,
            "success_text": "Exact discovery.",
            "failure_text": "Exact consequence.",
            "origin": origin,
        }
    ]
    pdf_store.put("spine", spine)
    pdf_store.put("bundles", bundles)
    pdf_store.put(
        "findings",
        [
            {
                "owner": "test",
                "value": {
                    "id": "chapter_detail",
                    "severity": "material",
                    "record_ids": ["ch_bell_content"],
                    "evidence": "A conditional source reward is missing.",
                    "instruction": "Restore the conditional reward.",
                },
            }
        ],
    )

    if review_provenance:
        evidence = {"source_evidence": [{"physical_page": extra_page}]}
        pdf_store.dispatch(
            "retained_critic",
            "final-review-probe",
            {"input": [{}, {"content": [{"text": json.dumps(evidence)}]}]},
        )
        pdf_store.response("retained_critic", {"id": "resp_critic", "status": "completed"})
        pdf_store.accept(
            "retained_critic",
            {
                "covered_ids": ["ch_bell_content"],
                "findings": [pdf_store.get("findings")[0]["value"]],
            },
        )

    class Repair:
        async def call(self, node, prompt, schema, evidence, images=None):
            partial = deepcopy(chapter)
            partial["new_entities"] = []
            partial["items"] = []
            partial["checks"] = []
            partial["dm_guidance"] = ""
            partial["source_notes"] = "Correction: reward is conditional on the escort."
            if extra_page is not None:
                partial["source_pages"].append(extra_page)
            return Patch(chapters=[partial])

    if extra_page is not None and not (review_provenance and extra_page == 2):
        with pytest.raises(NeedsAttention, match="nonexistent or unsupplied"):
            await Workflow(pdf_store, semantic=Repair()).repair({"slug": pdf_store.slug})
        assert pdf_store.get("bundles") == bundles
        return
    await Workflow(pdf_store, semantic=Repair()).repair({"slug": pdf_store.slug})
    repaired = pdf_store.get("bundles")["chapters"][0]
    assert repaired["source_notes"].startswith(
        "Accepted correction supersedes any conflicting earlier guidance.\n\nCorrection:"
    )
    assert chapter["source_notes"] in repaired["source_notes"]
    for field in (
        "dm_guidance",
        "source_pages",
        "events",
        "goals",
        "new_entities",
        "items",
        "checks",
    ):
        assert repaired[field] == chapter[field]
    with pdf_store.db() as db:
        normalization = json.loads(
            db.execute(
                "SELECT payload FROM artifact WHERE name LIKE 'repair-normalized-%'"
            ).fetchone()[0]
        )
    assert normalization["chapters"][0]["source_notes_mode"] == "paid_correction_layer"

    normalized = normalization["chapters"][0]
    if review_provenance:
        assert normalized["additional_source_pages"] == [2]
        assert normalized["retained_review_page_sources"] == [
            {
                "task_id": "retained_critic",
                "response_id": "resp_critic",
                "finding_ids": ["chapter_detail"],
                "pages": [2],
            }
        ]
