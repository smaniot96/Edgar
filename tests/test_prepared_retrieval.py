"""Use the actual scene reader and prepared projector without a provider or index write."""

from contextlib import contextmanager
from types import SimpleNamespace

from test_prepared_campaign import _approved, _fixture

from db.vector.retrieval import retrieve_active_scene


def test_runtime_extension_is_current_scene_gated_and_has_no_fabricated_pdf_page(monkeypatch):
    manifest, prepared = _fixture()
    approved = _approved(manifest, prepared)
    passage = approved.passages[0]
    payload = {
        "source": approved.source, "source_sha256": approved.source_sha256,
        "extraction_sha256": approved.extraction_sha256, "rules_edition": "2014",
        "manifest_sha256": approved.reviewed_sha256,
        "passage_id": passage.passage_id, "page": passage.page, "page_end": passage.page,
        "source_pages": [passage.page], "text_sha256": passage.text_sha256,
        "audience": passage.audience, "scene_ids": passage.scene_ids,
        "entity_ids": passage.entity_ids, "requires": passage.requires,
        "span_start": passage.start, "span_end": passage.end,
        "text": "The keeper is in danger.",
    }

    @contextmanager
    def active(_client, _collection, query_filter):
        yield query_filter

    class Client:
        def scroll(self, **_kwargs):
            return [SimpleNamespace(payload=payload)], None

    monkeypatch.setattr("db.vector.retrieval.active_index_read", active)
    hidden = retrieve_active_scene({"module": approved.model_dump()}, "dock", {}, client=Client())
    assert [chunk["passage_id"] for chunk in hidden["adventure_context"]] == ["source_intro"]
    visible = retrieve_active_scene(
        {"module": approved.model_dump()}, "dock", {"gate_open": "yes"}, client=Client(),
    )
    extension = visible["adventure_context"][-1]
    assert extension["passage_id"] == "dm_note"
    assert extension["source_kind"] == "dm_extension"
    assert extension["page"] is None and extension["source_pages"] == []
    assert extension["provenance_passage_ids"] == ["source_intro"]
