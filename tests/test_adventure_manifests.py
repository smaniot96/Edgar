"""Real synthetic PDF -> review -> disposable index; no live storage or provider calls."""

import json
from contextlib import closing

import pymupdf
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

from db.vector.embeddings import VECTOR_SIZE
from db.vector.retrieval import _chunk_from_payload, retrieve_active_scene
from edgar_core.manifests import (
    AdventureManifest,
    ManifestError,
    load_published_manifest,
    manifest_digest,
    require_published,
    select_scene_context,
)
from ingestion import embed
from ingestion.extract import extract_document, save_extraction
from ingestion.manifest import compile_manifest, manifest_chunks, review_manifest
from ingestion.readiness import prepare_report, report_summary


@pytest.fixture
def source_bundle(tmp_path, monkeypatch):
    from apps.api.services import adventures_meta as store

    pdf_dir, adventure_dir = tmp_path / "pdfs", tmp_path / "adventures"
    pdf_dir.mkdir()
    pdf = pdf_dir / "branching.pdf"
    with pymupdf.open() as doc:
        for n in range(1, 31):
            page = doc.new_page()
            page.insert_text((40, 60), f"LOCATION {n}", fontsize=16)
            page.insert_text(
                (40, 90),
                "\n".join(
                    f"Public detail {i} at location {n}: the stone path winds between old houses."
                    for i in range(5)
                ),
            )
            page.insert_text(
                (40, 220),
                "\n".join(
                    f"PRIVATE fact {i} for location {n}: an unseen guardian keeps the concealed map."
                    for i in range(5)
                ),
            )
        doc.save(pdf)
    extraction = extract_document(pdf)
    extraction.require_valid()
    draft = compile_manifest(extraction, rules_edition="2014")
    assert all(p.audience == "unreviewed" for p in draft.passages)
    assert draft.opening_scene_id is None
    chunks = embed.chunk_pages(extraction.text_pages(), source="branching")
    report = prepare_report(extraction, source_kind="pdf", chunks=chunks)
    save_extraction(extraction, adventure_dir / "branching", readiness=report)
    monkeypatch.setattr(store, "PDF_DIR", pdf_dir)
    monkeypatch.setattr(store, "ADVENTURES_DIR", adventure_dir)
    store.save_meta(
        "branching",
        {
            "status": "ready",
            "rules_edition": "2014",
            "chunks": len(chunks),
            "ingestion": report_summary(report),
        },
    )
    return pdf, extraction, draft


def edited_manifest(extraction, draft):
    data = draft.model_dump()
    data["opening_scene_id"] = "village_square"
    renames = {
        "scene_p0001": "village_square",
        "scene_p0029": "peaceful_ending",
        "scene_p0030": "broken_oath_ending",
    }
    for scene in data["scenes"]:
        scene["scene_id"] = renames.get(scene["scene_id"], scene["scene_id"])
    for passage in data["passages"]:
        text = dict(extraction.text_pages())[passage["page"]][passage["start"] : passage["end"]]
        passage["audience"] = "planner" if "PRIVATE" in text else "player"
        passage["scene_ids"] = [renames.get(s, s) for s in passage["scene_ids"]]
        if passage["page"] == 1 and passage["audience"] == "planner":
            passage["entity_ids"] = ["old_guardian", "hidden_map", "sleeping_trap"]
        if passage["page"] in (29, 30):
            passage["entity_ids"] = ["peace" if passage["page"] == 29 else "ruin"]
    reference = next(p for p in data["passages"] if p["page"] == 2 and p["audience"] == "planner")
    reference["requires"] = {"map_found": "discovered"}
    data["scenes"][0]["references"] = [reference["passage_id"]]
    data["scenes"][0]["links"] = [
        {"scene_id": "scene_p0002", "requires": {}},
        {"scene_id": "scene_p0003", "requires": {"map_found": "discovered"}},
    ]
    data["entities"] = [
        {"entity_id": "old_guardian", "kind": "actor", "title": "The guardian"},
        {
            "entity_id": "hidden_map",
            "kind": "clue",
            "title": "The hidden map",
            "discovery_flag": "map_found",
        },
        {"entity_id": "sleeping_trap", "kind": "hazard", "title": "Sleeping trap"},
        {"entity_id": "peace", "kind": "ending", "title": "Peaceful ending"},
        {"entity_id": "ruin", "kind": "ending", "title": "Broken oath ending"},
    ]
    return AdventureManifest.model_validate(data)


def approve(extraction, draft):
    return review_manifest(
        extraction, draft, reviewed_digest=manifest_digest(draft), reviewer="Fixture reviewer"
    )


@pytest.fixture
def isolated_index(monkeypatch):
    with closing(QdrantClient(":memory:")) as client:
        monkeypatch.setattr(embed, "get_qdrant_client", lambda: client)
        monkeypatch.setattr(
            embed,
            "get_embeddings",
            lambda texts: [[1.0] + [0.0] * (VECTOR_SIZE - 1) for _ in texts],
        )
        monkeypatch.setattr(embed, "BATCH_DELAY_SEC", 0)
        yield client


def test_long_pdf_compile_edit_publish_and_active_state_retrieval(source_bundle, isolated_index):
    from apps.api.routers.adventure_manifests import router
    from apps.api.services import adventures_meta as store

    _, extraction, draft = source_bundle
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as http:
        compiled = http.post("/adventures/branching/manifest/compile")
        assert compiled.status_code == 200
        assert "PRIVATE" in str(compiled.json()["passages"])
        rejected = http.post(
            "/adventures/branching/manifest/publish",
            json={
                "reviewed_digest": compiled.json()["reviewed_digest"],
                "reviewer": "Reader",
            },
        )
        assert rejected.status_code == 409 and "unreviewed" in rejected.text
        edited = edited_manifest(extraction, draft)
        response = http.put("/adventures/branching/manifest", json=edited.model_dump())
        assert response.status_code == 200
        published = http.post(
            "/adventures/branching/manifest/publish",
            json={
                "reviewed_digest": response.json()["reviewed_digest"],
                "reviewer": "Reader",
            },
        )
        assert published.status_code == 200, published.text
    metadata = store.load_meta("branching")
    assert "manifest_draft" not in metadata
    assert "PRIVATE" not in json.dumps(metadata["manifest"])
    manifest = load_published_manifest(metadata)
    assert {e.kind for e in manifest.entities} == {"actor", "clue", "hazard", "ending"}
    assert {e.entity_id for e in manifest.entities if e.kind == "ending"} == {"peace", "ruin"}
    points, _ = isolated_index.scroll("branching", limit=500, with_payload=True)
    assert len(points) == len(manifest.passages)
    assert {p.payload["passage_id"] for p in points} == {p.passage_id for p in manifest.passages}
    assert any(p.payload.get("scene_id") == "broken_oath_ending" for p in points)
    current = retrieve_active_scene(
        {"branching": manifest.model_dump()}, "village_square", {}, client=isolated_index
    )
    assert current["adventure_context"] and current["planner_context"]
    assert all("PRIVATE" not in c["text"] and c["page"] == 1 for c in current["adventure_context"])
    assert all(c["page"] == 1 for c in current["planner_context"])
    assert current["allowed_scene_ids"] == ["village_square", "scene_p0002"]
    unlocked = retrieve_active_scene(
        {"branching": manifest.model_dump()},
        "village_square",
        {"map_found": "discovered"},
        client=isolated_index,
    )
    assert {c["page"] for c in unlocked["planner_context"]} == {1, 2}
    assert "scene_p0003" in unlocked["allowed_scene_ids"]
    assert not any(
        c["page"] >= 3 for c in unlocked["planner_context"] + unlocked["adventure_context"]
    )
    chunk = unlocked["planner_context"][0]
    assert chunk["source_sha256"] == extraction.source_sha256
    assert chunk["extraction_sha256"] == extraction.text_sha256()
    assert chunk["manifest_sha256"] == manifest.reviewed_sha256
    assert chunk["source_pages"] == [1] and chunk["rules_edition"] == "2014"
    assert chunk["entity_ids"] == ["old_guardian", "hidden_map", "sleeping_trap"]


@pytest.mark.parametrize(
    "fault", ["overlap", "gap", "unknown_link", "duplicate_id", "unreviewed", "missing_opening"]
)
def test_ambiguous_or_incomplete_review_rejected(source_bundle, fault):
    _, extraction, draft = source_bundle
    data = edited_manifest(extraction, draft).model_dump()
    if fault == "overlap":
        data["passages"][1]["start"] = data["passages"][0]["start"]
    elif fault == "gap":
        data["passages"].pop(0)
    elif fault == "unknown_link":
        data["scenes"][0]["links"][0]["scene_id"] = "made_up"
    elif fault == "duplicate_id":
        data["scenes"][1]["scene_id"] = data["scenes"][0]["scene_id"]
    elif fault == "unreviewed":
        data["passages"][-1]["audience"] = "unreviewed"
    else:
        data["opening_scene_id"] = None
    with pytest.raises(ValueError):
        approve(extraction, AdventureManifest.model_validate(data))


def test_stale_receipts_source_versions_and_inspected_drafts_rejected(source_bundle, monkeypatch):
    pdf, extraction, draft = source_bundle
    edited = edited_manifest(extraction, draft)
    with pytest.raises(ManifestError, match="inspected manifest changed"):
        review_manifest(
            extraction, edited, reviewed_digest=manifest_digest(draft), reviewer="Reader"
        )
    published = approve(extraction, edited)
    changed = published.model_dump()
    changed["passages"][0]["audience"] = "planner"
    with pytest.raises(ManifestError, match="changed after review"):
        require_published(changed)
    with pytest.raises(ManifestError, match="stale source_sha256"):
        require_published(published, {"source_sha256": "f" * 64})
    monkeypatch.setattr(
        embed, "_embed_and_upsert", lambda *a, **kw: pytest.fail("stale source must not write")
    )
    with pdf.open("ab") as f:
        f.write(b"\nchanged source bytes\n")
    with pytest.raises(ValueError, match="does not match this PDF"):
        embed.run_pipeline(
            pdf,
            "branching",
            reviewed_manifest=published.model_dump(),
            reviewed_extraction=pdf.parent.parent
            / "adventures/branching/branching.extraction.json",
        )


def test_missing_indexed_passage_and_ambiguous_binding_fail_closed(source_bundle, isolated_index):
    _, extraction, draft = source_bundle
    manifest = approve(extraction, edited_manifest(extraction, draft))
    chunks = manifest_chunks(extraction, manifest)
    embed._embed_and_upsert(chunks, "branching")
    with pytest.raises(ManifestError, match="exactly one"):
        retrieve_active_scene(
            {"first": manifest.model_dump(), "second": manifest.model_dump()},
            "village_square",
            {},
            client=isolated_index,
        )
    target = select_scene_context(manifest, "village_square")["player_passage_ids"][0]
    isolated_index.delete(
        "branching",
        FilterSelector(
            filter=Filter(must=[FieldCondition(key="passage_id", match=MatchValue(value=target))])
        ),
    )
    with pytest.raises(ManifestError, match="missing or stale"):
        retrieve_active_scene(
            {"branching": manifest.model_dump()}, "village_square", {}, client=isolated_index
        )


def test_legacy_metadata_is_explicitly_unreviewed():
    with pytest.raises(ManifestError, match="reviewed scene manifest"):
        load_published_manifest({"status": "ready", "chunks": 100})
    chunk = _chunk_from_payload(
        {"text": "Legacy secret", "scene_id": "legacy_room"}, kind="adventure"
    )
    assert chunk["scene_id"] == "legacy_room" and "audience" not in chunk


def test_mutated_payload_text_and_wrong_adventure_edition_rejected(source_bundle, isolated_index):
    _, extraction, draft = source_bundle
    manifest = approve(extraction, edited_manifest(extraction, draft))
    embed._embed_and_upsert(manifest_chunks(extraction, manifest), "branching")
    with pytest.raises(ManifestError, match="edition"):
        retrieve_active_scene(
            {"branching": manifest.model_dump()},
            "village_square",
            client=isolated_index,
            rules_edition="2024",
        )
    passage = next(p for p in manifest.passages if p.page == 1 and p.audience == "player")
    isolated_index.set_payload(
        "branching",
        {"text": "The future secret villain is the king."},
        Filter(must=[FieldCondition(key="passage_id", match=MatchValue(value=passage.passage_id))]),
    )
    with pytest.raises(ManifestError, match="differs from its reviewed manifest"):
        retrieve_active_scene(
            {"branching": manifest.model_dump()}, "village_square", client=isolated_index
        )


def test_generated_manuscript_review_uses_existing_bundle_without_pdf(
    tmp_path, monkeypatch, isolated_index
):
    from apps.api.services import adventure_manifests as service, adventures_meta as store
    from ingestion.extract import ExtractedPage, Extraction
    from ingestion.quality import check_text

    monkeypatch.setattr(store, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(store, "PDF_DIR", tmp_path / "pdfs")
    text = (
        "The old forest road leads to a quiet village where a friendly guide waits beside the well."
    )
    extraction = Extraction(
        "authored", "", 1, [ExtractedPage(1, text, "generated", check_text(text))]
    )
    extraction.source_sha256 = extraction.text_sha256()
    report = prepare_report(
        extraction, source_kind="generated", chunks=[{"text": text, "page": 1, "source_pages": [1]}]
    )
    save_extraction(extraction, store.ADVENTURES_DIR / "authored", readiness=report)
    store.save_meta("authored", {"status": "ready", "ingestion": report_summary(report)})
    compiled = service.compile_draft("authored")
    draft = AdventureManifest.model_validate(compiled["manifest"])
    draft.opening_scene_id = draft.scenes[0].scene_id
    for passage in draft.passages:
        passage.audience = "player"
    inspected = service.edit_draft("authored", draft)
    result = service.publish_manifest(
        "authored", reviewed_digest=inspected["reviewed_digest"], reviewer="Author"
    )
    assert result["status"] == "ready"
    manifests = service.campaign_manifests(["authored"])
    assert (
        retrieve_active_scene(manifests, draft.opening_scene_id, client=isolated_index)[
            "adventure_context"
        ][0]["text"]
        == text
    )
    preview = store.ADVENTURES_DIR / "authored/authored.md"
    preview.write_text(preview.read_text() + "\nChanged manuscript")
    with pytest.raises(ValueError, match="changed"):
        service.inspect_manifest("authored")


def test_failed_publication_retains_reviewable_draft_without_certifying_index(
    source_bundle, monkeypatch
):
    from apps.api.services import adventure_manifests as service, adventures_meta as store

    _, extraction, draft = source_bundle
    edited = service.edit_draft("branching", edited_manifest(extraction, draft))

    def fail(*args, **kwargs):
        raise RuntimeError("simulated embedding failure")

    monkeypatch.setattr(embed, "_embed_and_upsert", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        service.publish_manifest(
            "branching", reviewed_digest=edited["reviewed_digest"], reviewer="Reader"
        )
    metadata = store.load_meta("branching")
    assert metadata["status"] == "failed" and "manifest" not in metadata
    assert metadata["manifest_draft"]["reviewed_sha256"] is None


def test_scene_and_discovery_effects_are_bound_to_reviewed_current_state(source_bundle):
    from agent.models.adjudication import AdjudicationResult, FlagUpdate
    from agent.nodes.manifest_effects import enforce_manifest_effects

    _, extraction, draft = source_bundle
    manifest = approve(extraction, edited_manifest(extraction, draft))
    selection = select_scene_context(manifest, "village_square")
    state = {
        "manifest_status": "reviewed",
        "adventure_manifests": {"branching": manifest.model_dump()},
        "allowed_scene_ids": selection["allowed_scene_ids"],
        "discovery_entities": selection["entities"],
    }
    adj = AdjudicationResult(
        success=True,
        scene_id="broken_oath_ending",
        flags_set=[FlagUpdate(key="map_found", value="discovered")],
        flags_cleared=["map_found"],
    )
    accepted = enforce_manifest_effects(adj, state)
    assert accepted.scene_id is None and accepted.flags_cleared == []
    assert accepted.flags_set[0].key == "map_found"
    failed = enforce_manifest_effects(adj.model_copy(update={"success": False}), state)
    assert failed.flags_set == []
    elsewhere = enforce_manifest_effects(adj, {**state, "discovery_entities": []})
    assert elsewhere.flags_set == []
    branch = enforce_manifest_effects(adj.model_copy(update={"scene_id": "scene_p0002"}), state)
    assert branch.scene_id == "scene_p0002"


def test_manifest_rejects_a_conflicting_reviewed_extraction_edition(source_bundle):
    _, extraction, draft = source_bundle
    extraction.rules_edition = "2024"
    with pytest.raises(ManifestError, match="edition conflicts"):
        approve(extraction, edited_manifest(extraction, draft))
