"""Original evidence fits according to the whole model request, not old token cutoffs."""

from __future__ import annotations

import hashlib

import pytest
from PIL import Image
from test_campaign_chapter_calls_v2 import ChapterResponses, pdf_store as pdf_store, workflow

from apps.api.services.campaign_prep import provider
from apps.api.services.campaign_prep.source import token_count
from apps.api.services.campaign_prep.store import NeedsAttention
from edgar_core.campaign_semantics import Spine


@pytest.mark.asyncio
async def test_complete_original_pages_above_old_thresholds_remain_in_single_chapter_calls(
    pdf_store,
):
    source = pdf_store.get("source")
    original = "The guard keeps the iron key below the bridge. " * 20_000
    original += "FINAL ORIGINAL SOURCE MARKER: violet seven-pointed star."
    assert token_count(original) > 180_000
    source["pages"][2]["text"] = original
    source["pages"][2]["raw_sha256"] = hashlib.sha256(original.encode()).hexdigest()
    pdf_store.put("source", source)
    responses = ChapterResponses(pdf_store)
    active = workflow(pdf_store, responses)
    address = {"slug": pdf_store.slug}
    await active.source(address)
    await active.spine(address)
    await active.bundles(address)

    assert [r["schema"] for r in responses.requests] == ["Spine", "Bundles", "Bundles"]
    planning, first, second = responses.requests
    for request, key in (
        (planning, "source_pages"),
        (first, "source_evidence"),
        (second, "source_evidence"),
    ):
        assert (
            next(p["text"] for p in request["evidence"][key] if p["physical_page"] == 3) == original
        )
        assert "source_readings" not in request["evidence"]
    assert pdf_store.get("spine-context")["source_mode"] == "original-pages"
    for key in ("original_request", "submitted_request"):
        assert pdf_store.get("spine-context")[key]["fits"]
    for chapter in ("ch_bell", "ch_after"):
        receipt = pdf_store.get("chapter-context-" + chapter)
        for key in ("original_request_context", "submitted_request_context"):
            assert receipt[key]["fits"]
            assert receipt[key]["estimated_input_tokens"] > 180_000


def test_context_accounts_for_instructions_schema_and_actual_image_dimensions(tmp_path):
    evidence = {"source_pages": [{"text": "The original source is complete."}]}
    small, large = tmp_path / "small.png", tmp_path / "large.png"
    Image.new("RGB", (32, 32)).save(small)
    Image.new("RGB", (320, 640)).save(large)
    text = provider.request_context("spine", Spine, evidence)
    measured = provider.request_context("spine", Spine, evidence, [small, large])
    assert text["text_tokens"] > token_count(evidence)
    assert measured["image_tokens_estimate"] == provider.image_token_estimate(
        small
    ) + provider.image_token_estimate(large)
    assert provider.image_token_estimate(large) > provider.image_token_estimate(small)
    assert measured["estimated_input_tokens"] > text["estimated_input_tokens"]
    assert measured["max_input_tokens"] == 922_000
    assert measured["context_window"] == 1_050_000
    assert measured["output_reserve"] == 128_000
    assert measured["fits"]


@pytest.mark.asyncio
async def test_true_context_overflow_prevents_dispatch_and_provider_submission(
    pdf_store, monkeypatch
):
    responses = ChapterResponses(pdf_store)
    active = workflow(pdf_store, responses)
    evidence = {"source_pages": [{"text": "Original source page."}]}
    context = provider.request_context("spine", Spine, evidence)
    monkeypatch.setattr(provider, "MODEL_INPUT_TOKENS", context["estimated_input_tokens"] - 1)
    with pytest.raises(NeedsAttention, match="exceeds the model's input context"):
        await active.semantic.call("spine", "spine", Spine, evidence)
    assert responses.requests == []
    with pdf_store.db() as db:
        assert db.execute("SELECT count(*) FROM task").fetchone()[0] == 0
