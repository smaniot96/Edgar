"""Focused replacement contract checks. No DB containers or paid provider calls."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep.compile import assemble, closure, make_extraction
from apps.api.services.campaign_prep.provider import SemanticClient
from apps.api.services.campaign_prep.source import acquire_pdf
from apps.api.services.campaign_prep.store import NeedsAttention, Store
from edgar_core.campaign_semantics import Review
from edgar_core.manifests import AdventureManifest, manifest_digest, require_published
from ingestion.manifest import manifest_chunks

ORIGIN = {"kind": "authored", "pages": [], "regions": [], "note": "", "uncertain": False}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    value = Store("fresh")
    value.initialize({"source_kind": "ai", "rules_edition": "2014"})
    return value


def sample():
    spine = {
        "title": "The Quiet Bell",
        "premise": "A missing keeper threatens the town's autumn festival.",
        "setting": "A small river town.",
        "hook": "The bell has fallen silent.",
        "ending": "The keeper is safe and the community chooses how to keep its promise.",
        "level_recommendation": "",
        "dm_guidance": "Let the hero decide how to approach the keeper and the witnesses.",
        "chapters": [
            {
                "id": "ch_bell",
                "title": "The silent bell",
                "brief": "Find and help the missing keeper.",
                "source_order": 1,
                "play_order": 1,
                "chronology": "Before the festival",
                "pages": [],
                "entity_ids": ["place_square"],
                "secret_ids": [],
                "depends_on": [],
            }
        ],
        "entities": [
            {
                "id": "place_square",
                "kind": "place",
                "name": "The Square",
                "aliases": [],
                "description": "A riverside square with a bell tower.",
                "motive": "",
                "knowledge": "",
                "origin": ORIGIN,
            }
        ],
        "secrets": [],
    }
    bundles = {
        "chapters": [
            {
                "chapter_id": "ch_bell",
                "goals": [
                    {
                        "id": "goal_keeper",
                        "title": "The keeper is safe",
                        "objective": "Help the keeper return safely or choose a safe refuge.",
                        "completion": "The keeper is safe through any credible intervention or agreement.",
                        "required": True,
                        "event_ids": ["event_square"],
                        "depends_on": [],
                        "origin": ORIGIN,
                    }
                ],
                "events": [
                    {
                        "id": "event_square",
                        "title": "A silent square",
                        "location_id": "place_square",
                        "participants": [],
                        "goal_ids": ["goal_keeper"],
                        "secret_ids": [],
                        "clue_ids": [],
                        "trigger": "The hero arrives.",
                        "timing": "Before sunset; the festival can be delayed if necessary.",
                        "public_description": "A bell hangs motionless above a quiet riverside square. A torn blue ribbon flutters beside the tower door.",
                        "dm_notes": "The ribbon belongs to the keeper. A witness near the river can explain where it was lost. Offer clues through conversation or exploration. Delaying the festival changes local reactions but never prevents a credible rescue. Do not prescribe a check or spell.",
                        "next_event_ids": [],
                        "encounters": [],
                        "origin": ORIGIN,
                    }
                ],
                "new_entities": [],
                "new_secrets": [],
                "dm_guidance": "Give the hero useful clues and let unexpected solutions work.",
            }
        ]
    }
    return spine, bundles


def test_local_compilation_uses_one_goal_and_no_scripted_prepared_contract():
    spine, bundles = sample()
    result = assemble("fresh", "2014", spine, bundles, [])
    manifest = AdventureManifest.model_validate(result["manifest"])
    manifest.reviewed_by = "independent-review"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    require_published(manifest)
    chunks = manifest_chunks(make_extraction(result), manifest)
    assert len(manifest.objectives) == 1 and not manifest.milestones
    assert manifest.prepared is None
    assert len(chunks) == 3
    assert chunks[0]["audience"] == "player"
    assert chunks[-1]["audience"] == "planner"
    assert "credible" in manifest.objectives[0].completion_condition


def test_dependency_closure_keeps_unrelated_records():
    registry = {
        "secret_a": {"kind": "secret", "value": {"known_by": ["npc_a"]}},
        "npc_a": {"kind": "entity", "value": {}},
        "npc_b": {"kind": "entity", "value": {}},
    }
    assert closure(registry, {"secret_a"}) == ["npc_a", "secret_a"]


@pytest.mark.asyncio
async def test_completed_response_replays_after_store_reopen_without_create(store):
    calls = []
    response = {
        "id": "resp-local-probe",
        "model": "gpt-6-luna",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps({"covered_ids": [], "findings": []})}
                ],
            }
        ],
        "usage": {"input_tokens": 20, "output_tokens": 8},
    }

    class Responses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            # Dispatch is already durable before provider invocation.
            with store.db() as db:
                assert (
                    db.execute("SELECT count(*) FROM task WHERE state='dispatching'").fetchone()[0]
                    == 1
                )
            return SimpleNamespace(model_dump=lambda **_: response)

    client = SimpleNamespace(responses=Responses())
    first = await SemanticClient(store, client).call("probe", "review", Review, {"review_ids": []})
    with store.db() as db:
        db.execute(
            "UPDATE task SET accepted=NULL"
        )  # Simulate restart after response persisted, before acceptance.
    second = await SemanticClient(Store(store.slug), client).call(
        "probe", "review", Review, {"review_ids": []}
    )
    assert first == second and len(calls) == 1
    assert calls[0]["reasoning"] == {"effort": "medium"}
    assert calls[0]["model"] == "gpt-6-luna"
    assert "max_output_tokens" not in calls[0]
    assert store.usage()["call_count"] == 1


@pytest.mark.asyncio
async def test_ambiguous_dispatch_never_blindly_resubmits(store):
    class Responses:
        async def create(self, **kwargs):
            raise RuntimeError("connection lost before receipt")

    client = SimpleNamespace(responses=Responses())
    with pytest.raises(RuntimeError):
        await SemanticClient(store, client).call("probe", "review", Review, {"review_ids": []})
    with pytest.raises(NeedsAttention, match="Ambiguous"):
        await SemanticClient(Store(store.slug), client).call(
            "probe", "review", Review, {"review_ids": []}
        )
    assert store.usage()["call_count"] == 1


def test_pdf_renders_and_ocrs_every_page_even_with_native_text(store, tmp_path):
    pdf = tmp_path / "fresh.pdf"
    with fitz.open() as doc:
        for text in (
            "A silent bell above the village square.",
            "The keeper waits at the old mill.",
        ):
            page = doc.new_page()
            page.insert_text((72, 72), text, fontsize=18)
        doc.save(pdf)
    result = acquire_pdf(store, pdf)
    assert len(result["pages"]) == 2
    for page in result["pages"]:
        assert Path(result["directory"], page["image"]).is_file()
        assert page["words"] and page["regions"] and page["raw_sha256"]
        assert page["quality"]["word_count"] > 4
    assert acquire_pdf(store, pdf) == result


@pytest.mark.asyncio
async def test_archived_preparation_never_dispatches(store, monkeypatch):
    from apps.api.services.campaign_prep.runtime import (
        prepare_adventure,
        recover_private_preparations,
    )

    adventures_meta.save_meta(
        "old",
        {"private_preparation": True, "status": "failed", "preparation_run_id": "prep-" + "a" * 32},
    )
    before = adventures_meta.meta_path("old").read_bytes()
    await recover_private_preparations()
    with pytest.raises(ValueError, match="held"):
        await prepare_adventure("old")
    assert adventures_meta.meta_path("old").read_bytes() == before
