"""Critical paid-response durability and cancellation tests with a local provider stub."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep import provider
from apps.api.services.campaign_prep.provider import SemanticClient
from apps.api.services.campaign_prep.store import Cancelled, NeedsAttention, Store
from edgar_core.campaign_semantics import Bundles, Review, Spine


def receipt(status="completed", *, text=None, metadata=None, model="gpt-6-luna"):
    result = {
        "id": "resp_local_durable",
        "model": model,
        "status": status,
        "metadata": metadata or {},
        "output": [],
    }
    if status == "completed":
        result["output"] = [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": text if text is not None else '{"covered_ids": [], "findings": []}',
                    }
                ],
            }
        ]
        result["usage"] = {"input_tokens": 50, "output_tokens": 14}
    return result


def response(value):
    return SimpleNamespace(model_dump=lambda **_: value)


@pytest.fixture
def durable_store(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    value = Store("fresh_recovery")
    value.initialize({"source_kind": "ai", "rules_edition": "2014"})

    async def no_delay(_):
        return None

    monkeypatch.setattr(provider.asyncio, "sleep", no_delay)
    return value


def test_missing_provider_receipt_has_specific_safe_recovery_and_real_retained_progress(
    durable_store,
):
    from apps.api.services.campaign_prep.runtime import retry_preparation, safe_preparation_status

    durable_store.put("ai-outline", {"PRIVATE": "story contents"})
    durable_store.dispatch("a" * 64, "ai-chapter", {"PRIVATE": "request"})
    durable_store.update(status="blocked", error="PRIVATE connection error")
    meta = {"private_preparation": True, "preparation_workflow": "campaign-graph-2.0"}
    adventures_meta.save_meta(durable_store.slug, meta)
    projected = safe_preparation_status(meta, durable_store.slug)
    assert projected.failure.code == "provider_receipt_missing"
    assert projected.failure.recovery_action == "check_existing_result"
    assert not projected.retryable and projected.action == "none"
    assert projected.retained_work == "outline_and_chapters"
    assert (
        "PRIVATE" not in projected.model_dump_json() and "a" * 64 not in projected.model_dump_json()
    )
    before = durable_store.state()
    with pytest.raises(ValueError, match="Reconcile the existing result"):
        retry_preparation(durable_store.slug)
    assert durable_store.state() == before
    from apps.api.services.campaign_sources import safe_job_read

    meta["job"] = {"job_id": "b" * 32, "kind": "generation", "state": "failed", "retained_work": "none"}
    card = safe_job_read(meta, durable_store.slug)
    assert card.retained_work == "outline_and_chapters" and not card.retryable
    assert card.explanation == projected.failure.message and card.action_label is None


def test_old_wrapped_validation_error_projects_safe_retained_correction(durable_store):
    from apps.api.services.campaign_prep.runtime import safe_preparation_status

    durable_store.put("structural-repair-local", {"state": "rejected", "error": "PRIVATE source"})
    durable_store.update(status="blocked", error_type="NeedsAttention", error="PRIVATE source")
    meta = {"private_preparation": True, "preparation_workflow": "campaign-graph-2.0"}
    projected = safe_preparation_status(meta, durable_store.slug)
    assert projected.failure.code == "preparation_correction_rejected"
    assert projected.retryable and "PRIVATE" not in projected.model_dump_json()


def test_cancelled_preparation_card_does_not_claim_the_adventure_is_ready(durable_store):
    from apps.api.services.campaign_sources import safe_job_read

    durable_store.update(status="cancelled")
    meta = {"private_preparation": True, "preparation_workflow": "campaign-graph-2.0",
            "job": {"job_id": "b" * 32, "kind": "generation", "state": "cancelled"}}
    card = safe_job_read(meta, durable_store.slug)
    assert card.state == "cancelled" and not card.retryable
    assert "cancelled" in card.explanation and "ready to play" not in card.explanation


@pytest.mark.parametrize(("source", "retained"), [
    ({"kind": "ai"}, "none"),
    ({"kind": "pdf", "pages": []}, "source_pdf"),
    ({"kind": "pdf", "pages": [{"page": 1}]}, "inspected_extraction"),
])
def test_retained_progress_distinguishes_brief_pdf_and_inspection(durable_store, source, retained):
    from apps.api.services.campaign_prep.runtime import retained_preparation_work

    durable_store.put("source", source)
    assert retained_preparation_work(durable_store) == retained


@pytest.mark.asyncio
async def test_restart_resumes_known_response_instead_of_submitting_again(durable_store):
    calls = {"create": 0, "retrieve": 0}

    class Responses:
        async def create(self, **_):
            calls["create"] += 1
            return response(receipt("in_progress"))

        async def retrieve(self, response_id):
            assert response_id == "resp_local_durable"
            row = _task(durable_store)
            # Receipt identity must already be committed before the first poll/restart.
            assert json.loads(row["response"])["id"] == response_id
            calls["retrieve"] += 1
            if calls["retrieve"] == 1:
                raise asyncio.CancelledError()  # orderly worker shutdown, no provider cancellation
            return response(receipt())

    client = SimpleNamespace(responses=Responses())
    with pytest.raises(asyncio.CancelledError):
        await SemanticClient(durable_store, client).call("review_probe", "review", Review, {})
    resolved = await SemanticClient(Store(durable_store.slug), client).call(
        "review_probe", "review", Review, {}
    )
    assert resolved == Review(covered_ids=[])
    assert calls == {"create": 1, "retrieve": 2}
    assert durable_store.usage()["call_count"] == 1


@pytest.mark.asyncio
async def test_transient_poll_errors_keep_the_same_paid_response(durable_store):
    calls = {"create": 0, "retrieve": 0}

    class Responses:
        async def create(self, **_):
            calls["create"] += 1
            return response(receipt("queued"))

        async def retrieve(self, response_id):
            assert response_id == "resp_local_durable"
            calls["retrieve"] += 1
            request = httpx.Request("GET", "https://provider.invalid/responses/local")
            if calls["retrieve"] == 1:
                raise APIConnectionError(request=request)
            if calls["retrieve"] == 2:
                raise APIStatusError(
                    "retry later", response=httpx.Response(429, request=request), body=None
                )
            return response(receipt())

    result = await SemanticClient(durable_store, SimpleNamespace(responses=Responses())).call(
        "review_probe", "review", Review, {}
    )
    assert result.covered_ids == []
    assert calls == {"create": 1, "retrieve": 3}


@pytest.mark.asyncio
async def test_invalid_paid_output_is_retained_and_reparsed_without_create(durable_store):
    creates = []

    class Responses:
        async def create(self, **kwargs):
            creates.append(kwargs)
            return response(receipt(text='{"not_the_review_schema": true}'))

    client = SimpleNamespace(responses=Responses())
    for store in (durable_store, Store(durable_store.slug)):
        with pytest.raises(NeedsAttention, match="schema/code fix"):
            await SemanticClient(store, client).call("review_probe", "review", Review, {})
        row = _task(store)
        assert json.loads(row["response"])["status"] == "completed"
        assert not row["accepted"]
    assert len(creates) == 1


@pytest.mark.asyncio
async def test_foreign_response_cannot_reconcile_an_ambiguous_dispatch(durable_store):
    dispatch = "a" * 64
    durable_store.dispatch(dispatch, "review_probe", {"model": "gpt-6-luna"})

    class Responses:
        async def retrieve(self, response_id):
            return response(receipt(metadata={"edgar_dispatch_id": "b" * 64}))

    client = SemanticClient(durable_store, SimpleNamespace(responses=Responses()))
    with pytest.raises(NeedsAttention, match="does not belong"):
        await client.reconcile(dispatch, "resp_someone_else")
    assert durable_store.task(dispatch)["response"] is None


@pytest.mark.asyncio
async def test_known_response_is_cancelled_and_receipted_without_acceptance(durable_store):
    calls = {"create": 0, "cancel": 0}

    class Responses:
        async def create(self, **_):
            calls["create"] += 1
            durable_store.update(cancel_requested=True)
            return response(receipt("in_progress"))

        async def cancel(self, response_id):
            calls["cancel"] += 1
            assert response_id == "resp_local_durable"
            assert json.loads(_task(durable_store)["response"])["id"] == response_id
            return response(receipt("cancelled"))

    with pytest.raises(Cancelled):
        await SemanticClient(durable_store, SimpleNamespace(responses=Responses())).call(
            "review_probe", "review", Review, {}
        )
    row = _task(durable_store)
    assert json.loads(row["response"])["status"] == "cancelled"
    assert row["accepted"] is None
    assert calls == {"create": 1, "cancel": 1}


@pytest.mark.asyncio
async def test_unapproved_model_is_saved_but_never_accepted(durable_store):
    class Responses:
        async def create(self, **_):
            return response(receipt(model="gpt-6-sol"))

    with pytest.raises(NeedsAttention, match="different model"):
        await SemanticClient(durable_store, SimpleNamespace(responses=Responses())).call(
            "review_probe", "review", Review, {}
        )
    row = _task(durable_store)
    assert json.loads(row["response"])["model"] == "gpt-6-sol"
    assert row["accepted"] is None


@pytest.mark.asyncio
async def test_cancelled_run_recovers_failed_provider_cancellation_after_restart(durable_store):
    durable_store.dispatch("c" * 64, "review_probe", {"model": "gpt-6-luna"})
    durable_store.response("c" * 64, receipt("in_progress"))
    durable_store.update(cancel_requested=True, status="cancelled")
    calls = {"retrieve": 0, "cancel": 0}

    class Responses:
        async def create(self, **_):
            raise AssertionError("Cancellation recovery must never create paid work")

        async def retrieve(self, response_id):
            assert response_id == "resp_local_durable"
            calls["retrieve"] += 1
            return response(receipt("in_progress"))

        async def cancel(self, response_id):
            assert response_id == "resp_local_durable"
            calls["cancel"] += 1
            if calls["cancel"] == 1:
                raise APIConnectionError(
                    request=httpx.Request("POST", "https://provider.invalid/responses/local/cancel")
                )
            return response(receipt("cancelled"))

    client = SimpleNamespace(responses=Responses())
    assert await SemanticClient(durable_store, client).reconcile_cancellation() is True
    reopened = Store(durable_store.slug)
    assert await SemanticClient(reopened, client).reconcile_cancellation() is False
    assert calls == {"retrieve": 2, "cancel": 2}
    assert json.loads(_task(reopened)["response"])["status"] == "cancelled"
    assert _task(reopened)["accepted"] is None
    assert reopened.state()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_recovery_preserves_response_that_finished_during_shutdown(
    durable_store,
):
    durable_store.dispatch("d" * 64, "review_probe", {"model": "gpt-6-luna"})
    durable_store.response("d" * 64, receipt("queued"))
    durable_store.update(cancel_requested=True, status="cancelled")

    class Responses:
        async def create(self, **_):
            raise AssertionError("Cancellation recovery must never create paid work")

        async def retrieve(self, response_id):
            return response(receipt())

        async def cancel(self, response_id):
            raise AssertionError("Completed paid work must be saved without cancelling it")

    assert (
        await SemanticClient(
            Store(durable_store.slug), SimpleNamespace(responses=Responses())
        ).reconcile_cancellation()
        is False
    )
    row = _task(durable_store)
    assert json.loads(row["response"])["status"] == "completed"
    assert row["accepted"] is None
    assert durable_store.usage()["input_tokens"] == 50


@pytest.mark.asyncio
async def test_cancelled_preflight_has_no_dispatch_or_submission(durable_store):
    durable_store.update(cancel_requested=True)

    class Responses:
        async def create(self, **_):
            raise AssertionError("Cancelled preflight must not submit")

    with pytest.raises(Cancelled):
        await SemanticClient(durable_store, SimpleNamespace(responses=Responses())).call(
            "review_probe", "review", Review, {}
        )
    assert durable_store.usage()["call_count"] == 0


def test_local_identity_change_invalidates_transitive_consumers_only(durable_store):
    from apps.api.services.campaign_prep.graph import Workflow

    spine, bundles = _semantic_campaign()
    durable_store.put("spine", spine)
    durable_store.put("bundles", bundles)
    workflow = Workflow(durable_store, semantic=object())
    original = workflow.registry()
    fingerprints = {key: workflow.fingerprint(original, key) for key in original}
    spine["entities"][0]["motive"] = "Protect the keeper through a safe refuge."
    durable_store.put("spine", spine)
    changed = workflow.registry()
    assert workflow.fingerprint(changed, "event_a") != fingerprints["event_a"]
    assert workflow.fingerprint(changed, "secret_keeper") != fingerprints["secret_keeper"]
    assert workflow.fingerprint(changed, "event_b") == fingerprints["event_b"]
    assert workflow.fingerprint(changed, "goal_b") == fingerprints["goal_b"]


@pytest.mark.parametrize("source_kind", ["ai", "pdf"])
def test_fabricated_physical_source_provenance_is_rejected(source_kind):
    from apps.api.services.campaign_prep.compile import validate_semantics

    spine, bundles = _semantic_campaign()
    source = {"kind": source_kind, "pages": [{"page": 1, "regions": [{"id": "p1_b1"}]}]}
    origin = spine["entities"][0]["origin"]
    origin.update(kind="pdf", pages=[2], regions=["p2_b1"])
    with pytest.raises(NeedsAttention, match="fictitious PDF|nonexistent physical"):
        validate_semantics(spine, bundles, source)


def test_region_citation_must_belong_to_an_actual_cited_page():
    from apps.api.services.campaign_prep.compile import validate_semantics

    spine, bundles = _semantic_campaign()
    source = {"kind": "pdf", "pages": [{"page": 1, "regions": [{"id": "p1_b1"}]}]}
    spine["entities"][0]["origin"].update(kind="pdf", pages=[1], regions=["p1_b9"])
    with pytest.raises(NeedsAttention, match="region is absent"):
        validate_semantics(spine, bundles, source)


def _semantic_campaign():
    origin = {"kind": "authored"}
    spine = Spine.model_validate(
        {
            "title": "The keeper and the courier",
            "premise": "Two independent opportunities",
            "setting": "A river town",
            "hook": "The hero arrives",
            "ending": "Choose a safe future",
            "dm_guidance": "Keep opportunities open",
            "chapters": [
                {
                    "id": "ch_" + key,
                    "title": "Chapter " + key,
                    "brief": "An opportunity",
                    "source_order": index,
                    "play_order": index,
                }
                for index, key in enumerate(("a", "b"), 1)
            ],
            "entities": [
                {
                    "id": "keeper",
                    "kind": "npc",
                    "name": "Keeper",
                    "description": "A missing keeper",
                    "origin": origin,
                },
                *[
                    {
                        "id": "place_" + key,
                        "kind": "place",
                        "name": "Place " + key,
                        "description": "A local place",
                        "origin": origin,
                    }
                    for key in ("a", "b")
                ],
            ],
            "secrets": [
                {
                    "id": "secret_keeper",
                    "truth": "The keeper chose a refuge",
                    "known_by": ["keeper"],
                    "setup_chapters": ["ch_a"],
                    "reveal_chapters": ["ch_a"],
                    "reveal_condition": "The keeper trusts the hero",
                    "origin": origin,
                }
            ],
        }
    ).model_dump()
    bundles = Bundles.model_validate(
        {
            "chapters": [
                {
                    "chapter_id": "ch_" + key,
                    "dm_guidance": "Allow any credible approach",
                    "goals": [
                        {
                            "id": "goal_" + key,
                            "title": "A safe outcome",
                            "objective": "Help someone",
                            "completion": "An agreement or intervention makes them safe",
                            "event_ids": ["event_" + key],
                            "origin": deepcopy(origin),
                        }
                    ],
                    "events": [
                        {
                            "id": "event_" + key,
                            "title": "A meeting",
                            "location_id": "place_" + key,
                            "secret_ids": ["secret_keeper"] if key == "a" else [],
                            "goal_ids": ["goal_" + key],
                            "trigger": "The hero chooses to engage",
                            "timing": "Flexible",
                            "public_description": "A quiet meeting place awaits.",
                            "dm_notes": "Offer useful opportunities",
                            "origin": deepcopy(origin),
                        }
                    ],
                }
                for key in ("a", "b")
            ]
        }
    ).model_dump()
    return spine, bundles


def _task(store):
    with store.db() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM task")]
    assert len(rows) == 1
    return rows[0]
