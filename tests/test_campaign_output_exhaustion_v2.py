"""Exhausted output stays paid retained evidence, never an accepted/repeated response."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep import provider
from apps.api.services.campaign_prep.provider import (
    OutputExhausted,
    SemanticClient,
    completed_json_values,
    instructions,
    response_format,
)
from apps.api.services.campaign_prep.store import NeedsAttention, Store, digest
from edgar_core.campaign_semantics import SCHEMA_VERSION, Review


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    value = Store("output_recovery")
    value.initialize({"source_kind": "ai", "rules_edition": "2014"})
    return value


class Responses:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def create(self, **request):
        self.requests.append(deepcopy(request))
        return SimpleNamespace(model_dump=lambda **_: deepcopy(self.response))


def retained_response(*, status="incomplete", reason="max_output_tokens", text=None):
    if text is None:
        text = '{"covered_ids":["finished_record"],"findings":[' + " \n" * 2000
    return {
        "id": "resp_retained_exhaustion",
        "status": status,
        "model": "gpt-6-luna",
        "incomplete_details": {"reason": reason},
        "usage": {"input_tokens": 54321, "output_tokens": 128000, "total_tokens": 182321},
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ],
    }


@pytest.mark.asyncio
async def test_exhaustion_preserves_original_request_receipt_usage_and_exact_output_on_reopen(
    store,
):
    response = retained_response()
    responses = Responses(response)
    evidence = {"review_ids": ["finished_record"]}
    client = SemanticClient(store, SimpleNamespace(responses=responses))
    with pytest.raises(OutputExhausted) as first:
        await client.call("final-review", "review", Review, evidence)
    error = first.value
    assert isinstance(error, NeedsAttention)
    assert error.response_id == response["id"]
    assert error.response == response
    assert error.output_text == response["output"][1]["content"][0]["text"]
    row = store.task(error.task_id)
    assert row["state"] == "incomplete" and row["accepted"] is None
    assert json.loads(row["response"]) == response
    # The pre-existing exact request/fingerprint remains unchanged; no output cap.
    expected = {
        "model": "gpt-6-luna",
        "reasoning": {"effort": "medium"},
        "input": [
            {"role": "system", "content": instructions("review")},
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
        "text": {"format": response_format(Review)},
        "background": True,
        "store": True,
        "service_tier": "default",
    }
    assert json.loads(row["request"]) == expected
    assert error.task_id == digest(
        {"node": "final-review", "request": expected, "schema_version": SCHEMA_VERSION}
    )
    assert "max_output_tokens" not in responses.requests[0]
    usage = store.usage()
    assert usage["call_count"] == 1
    assert usage["calls"][0]["usage"] == response["usage"]

    reopened = Store(store.slug)
    with pytest.raises(OutputExhausted) as second:
        await SemanticClient(reopened, SimpleNamespace(responses=responses)).call(
            "final-review", "review", Review, evidence
        )
    assert second.value.task_id == error.task_id
    assert second.value.output_text == error.output_text
    assert reopened.task(error.task_id) == row
    assert {key: value for key, value in reopened.usage().items() if key != "elapsed_seconds"} == {
        key: value for key, value in usage.items() if key != "elapsed_seconds"
    }
    assert len(responses.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,reason",
    [("incomplete", "content_filter"), ("failed", "max_output_tokens"), ("cancelled", None)],
)
async def test_other_terminal_failures_keep_existing_attention_behavior(store, status, reason):
    response = retained_response(status=status, reason=reason)
    responses = Responses(response)
    client = SemanticClient(store, SimpleNamespace(responses=responses))
    with pytest.raises(NeedsAttention) as raised:
        await client.call("review", "review", Review, {})
    assert not isinstance(raised.value, OutputExhausted)
    assert len(responses.requests) == 1
    assert store.usage()["calls"][0]["usage"] == response["usage"]


@pytest.mark.asyncio
async def test_completed_response_still_accepts_and_replays_without_new_create(store):
    value = {"covered_ids": ["finished_record"], "findings": []}
    response = retained_response(status="completed", reason=None, text=json.dumps(value))
    responses = Responses(response)
    client = SemanticClient(store, SimpleNamespace(responses=responses))
    assert (await client.call("review", "review", Review, {})).model_dump() == value
    reopened = Store(store.slug)
    assert (
        await SemanticClient(reopened, SimpleNamespace(responses=responses)).call(
            "review", "review", Review, {}
        )
    ).model_dump() == value
    assert len(responses.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "incomplete"])
async def test_explicit_recovery_reuses_original_receipt_after_prompt_edit(
    store, monkeypatch, tmp_path, status
):
    evidence = {"source_pages": [{"page": 2, "text": "Original chapter OCR"}]}
    image = tmp_path / "original-page.png"
    image.write_bytes(b"original rendered page bytes")
    value = {"covered_ids": ["finished_record"], "findings": []}
    response = retained_response(status=status, text=json.dumps(value))
    responses = Responses(response)
    client = SemanticClient(store, SimpleNamespace(responses=responses))
    if status == "completed":
        await client.call("chapter:original", "review", Review, evidence, [image])
    else:
        with pytest.raises(OutputExhausted) as original:
            await client.call("chapter:original", "review", Review, evidence, [image])
        original_id = original.value.task_id
    with store.db() as db:
        original_row = dict(db.execute("SELECT * FROM task").fetchone())
    monkeypatch.setattr(provider, "instructions", lambda _: "Updated chapter recovery instructions")
    reopened = Store(store.slug)
    recovered = SemanticClient(reopened, SimpleNamespace(responses=responses))
    if status == "completed":
        assert (
            await recovered.call(
                "chapter:original", "review", Review, evidence, [image], reuse_saved_request=True
            )
        ).model_dump() == value
    else:
        with pytest.raises(OutputExhausted) as raised:
            await recovered.call(
                "chapter:original", "review", Review, evidence, [image], reuse_saved_request=True
            )
        assert raised.value.task_id == original_id
        assert raised.value.response == response
    assert len(responses.requests) == 1
    assert reopened.task(original_row["id"]) == original_row
    assert reopened.usage()["call_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["evidence", "image", "schema", "reasoning", "model", "node"])
async def test_explicit_recovery_never_adopts_different_source_or_semantic_contract(
    store, monkeypatch, tmp_path, change
):
    evidence = {"source_pages": [{"page": 2, "text": "Original OCR"}]}
    image = tmp_path / "page.png"
    image.write_bytes(b"original rendered image")
    response = retained_response()
    responses = Responses(response)
    client = SemanticClient(store, SimpleNamespace(responses=responses))
    with pytest.raises(OutputExhausted) as original:
        await client.call("chapter:original", "review", Review, evidence, [image])
    monkeypatch.setattr(provider, "instructions", lambda _: "Revised instructions")
    node, schema = "chapter:original", Review
    if change == "evidence":
        evidence = {"source_pages": [{"page": 2, "text": "Different OCR"}]}
    elif change == "image":
        image.write_bytes(b"different rendered image")
    elif change == "schema":

        class DifferentReview(Review):
            pass

        schema = DifferentReview
    elif change == "reasoning":
        monkeypatch.setattr(provider, "EFFORT", "high")
    elif change == "model":
        monkeypatch.setattr(provider, "MODEL", "gpt-6-luna-other")
        responses.response = {**response, "model": "gpt-6-luna-other"}
    else:
        node = "chapter:repair:original"
    with pytest.raises(OutputExhausted) as current:
        await client.call(node, "review", schema, evidence, [image], reuse_saved_request=True)
    assert current.value.task_id != original.value.task_id
    assert len(responses.requests) == 2
    assert store.usage()["call_count"] == 2


@pytest.mark.asyncio
async def test_saved_request_adoption_is_opt_in(store, monkeypatch):
    responses = Responses(retained_response())
    client = SemanticClient(store, SimpleNamespace(responses=responses))
    with pytest.raises(OutputExhausted):
        await client.call("chapter:original", "review", Review, {})
    monkeypatch.setattr(provider, "instructions", lambda _: "Revised prompt")
    with pytest.raises(OutputExhausted):
        await client.call("chapter:original", "review", Review, {})
    assert len(responses.requests) == 2


@pytest.mark.asyncio
async def test_explicit_recovery_refuses_multiple_matching_originals(store, monkeypatch):
    responses = Responses(retained_response())
    client = SemanticClient(store, SimpleNamespace(responses=responses))
    for prompt in ("First instructions", "Second instructions"):
        monkeypatch.setattr(provider, "instructions", lambda _, text=prompt: text)
        with pytest.raises(OutputExhausted):
            await client.call("chapter:original", "review", Review, {})
    monkeypatch.setattr(provider, "instructions", lambda _: "Current instructions")
    with pytest.raises(NeedsAttention, match="Multiple retained"):
        await client.call("chapter:original", "review", Review, {}, reuse_saved_request=True)
    assert len(responses.requests) == 2


@pytest.mark.asyncio
async def test_explicit_recovery_refuses_corrupted_original_request_identity(store, monkeypatch):
    responses = Responses(retained_response())
    client = SemanticClient(store, SimpleNamespace(responses=responses))
    with pytest.raises(OutputExhausted):
        await client.call("chapter:original", "review", Review, {})
    with store.db() as db:
        db.execute("UPDATE task SET id='corrupted-identity'")
    monkeypatch.setattr(provider, "instructions", lambda _: "Revised prompt")
    with pytest.raises(NeedsAttention, match="integrity check failed"):
        await client.call("chapter:original", "review", Review, {}, reuse_saved_request=True)
    assert len(responses.requests) == 1


@pytest.mark.asyncio
async def test_explicit_recovery_never_duplicates_ambiguous_original_submission(store, monkeypatch):
    responses = Responses(retained_response())
    client = SemanticClient(store, SimpleNamespace(responses=responses))
    with pytest.raises(OutputExhausted):
        await client.call("chapter:original", "review", Review, {})
    with store.db() as db:
        db.execute("UPDATE task SET response=NULL, state='dispatching'")
    monkeypatch.setattr(provider, "instructions", lambda _: "Revised prompt")
    with pytest.raises(NeedsAttention, match="Ambiguous provider submission"):
        await client.call("chapter:original", "review", Review, {}, reuse_saved_request=True)
    assert len(responses.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6-sol", "", None, "gpt-6-lunatic"])
async def test_exhausted_output_from_different_model_is_not_recoverable(store, model):
    response = {**retained_response(), "model": model}
    responses = Responses(response)
    client = SemanticClient(store, SimpleNamespace(responses=responses))
    with pytest.raises(NeedsAttention, match="different model") as raised:
        await client.call("chapter:original", "review", Review, {})
    assert not isinstance(raised.value, OutputExhausted)
    assert len(responses.requests) == 1
    with store.db() as db:
        row = dict(db.execute("SELECT * FROM task").fetchone())
    assert row["state"] == "incomplete" and row["accepted"] is None
    assert json.loads(row["response"]) == response


def test_partial_nested_chapter_retains_closed_fields_and_records_without_completing_parent():
    text = (
        '{"chapters":[{"chapter_id":"goblin_arrows","goals":[{"id":"rescue",'
        '"objective":"Help the captive"},{"id":"unfinished","objective":"Do not guess'
    )
    assert completed_json_values(text) == [
        {"path": ["chapters", 0, "chapter_id"], "value": "goblin_arrows"},
        {
            "path": ["chapters", 0, "goals", 0],
            "value": {"id": "rescue", "objective": "Help the captive"},
        },
        {"path": ["chapters", 0, "goals", 1, "id"], "value": "unfinished"},
    ]
    # Neither the unfinished goal nor its containing chapter is a complete record.
    assert all(len(row["path"]) > 2 for row in completed_json_values(text))


@pytest.mark.parametrize(
    "suffix",
    ['"unterminated', '"trailing escape\\', "1e+", "12.", "12", "tru", "true", "nul", "null"],
)
def test_truncated_scalar_never_becomes_a_completed_field(suffix):
    assert completed_json_values('{"kept":"complete","missing":' + suffix) == [
        {"path": ["kept"], "value": "complete"}
    ]


def test_delimited_numeric_literals_and_escaped_strings_are_retained_exactly():
    text = '{"values":[-1.25e+3,true,null,"a\\"b\\nc",{"id":"closed"},'
    assert completed_json_values(text) == [
        {"path": ["values", 0], "value": -1250.0},
        {"path": ["values", 1], "value": True},
        {"path": ["values", 2], "value": None},
        {"path": ["values", 3], "value": 'a"b\nc'},
        {"path": ["values", 4], "value": {"id": "closed"}},
    ]


def test_closed_json_before_whitespace_is_reported_as_closed_root_but_never_accepted_here():
    value = {"chapters": [{"chapter_id": "goblin_arrows", "goals": []}]}
    assert completed_json_values(json.dumps(value) + " \n" * 100_000) == [
        {"path": [], "value": value}
    ]


@pytest.mark.parametrize("text", ["", "   ", "```json\n{}", '{"value":NaN', '{"value":Infinity'])
def test_helper_does_not_repair_non_json_output(text):
    assert completed_json_values(text) == []
