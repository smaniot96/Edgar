"""Exercise the real LangChain/OpenAI request encoding with an in-memory HTTP transport."""

import json

import httpx
import pytest
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, AuthenticationError, RateLimitError
from openai.types.responses import Response
from pydantic import BaseModel
from test_preparation_supervisor import binding

from apps.api.services.preparation import PreparationTaskFailure
from apps.api.services.preparation_provider import invoke_preparation, request_cost_ceiling
from edgar_core.model_profiles import load_model_profiles
from edgar_core.preparation import PreparationStage, PreparationTask, canonical_sha256


@pytest.fixture(autouse=True)
def response_store(tmp_path, monkeypatch):
    monkeypatch.setattr("apps.api.services.adventures_meta.ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr("apps.api.services.preparation_responses.POLL_SECONDS", 0)


class Answer(BaseModel):
    text: str


def frozen():
    profile = load_model_profiles(environ={}).to_dict()
    return binding().model_copy(
        update={"profile_snapshot": profile, "profile_snapshot_sha256": canonical_sha256(profile)}
    )


def task(**updates):
    return PreparationTask(
        task_id="unit",
        stage=PreparationStage.P01,
        task_kind="commission",
        max_input_tokens=20000,
        max_output_tokens=100,
        max_cost_microusd=request_cost_ceiling(20000, 100),
        **updates,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("visual", [False, True])
async def test_actual_provider_request_is_sol_high_responses_without_sdk_retry(monkeypatch, visual):
    from agent import llm

    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_offline",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-6.1-sol",
                "error": None,
                "incomplete_details": None,
                "instructions": None,
                "metadata": {},
                "output": [
                    {
                        "id": "msg_offline",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": '{"text":"offline"}', "annotations": []}
                        ],
                    }
                ],
                "parallel_tool_calls": True,
                "tools": [],
                "tool_choice": "auto",
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 20,
                    "total_tokens": 70,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 5},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        constructors = []

        def build(**kwargs):
            constructors.append(kwargs)
            return ChatOpenAI(
                **kwargs, http_async_client=client, base_url="https://offline.invalid/v1"
            )

        monkeypatch.setattr(llm, "OPENAI_API_KEY", "test-offline-never-network")
        monkeypatch.setattr(llm, "ChatOpenAI", build)
        planned = task()
        images = ()
        if visual:
            import io

            from PIL import Image

            pixels = io.BytesIO()
            Image.new("RGB", (16, 16), color="white").save(pixels, format="PNG")
            images = ((pixels.getvalue(), "image/png"),)
            planned = planned.model_copy(
                update={
                    "max_input_tokens": 60000,
                    "max_cost_microusd": request_cost_ceiling(60000, 100),
                }
            )
        parsed, usage = await invoke_preparation(
            frozen(),
            planned,
            Answer,
            instruction="Return the assigned text.",
            evidence={"source": "untrusted"},
            idempotency_key="offline-key",
            images=images,
        )
    assert parsed.text == "offline"
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/responses"
    assert body["model"] == "gpt-6.1-sol"
    assert body["reasoning"]["effort"] == "high"
    assert body["max_output_tokens"] == 100
    assert "temperature" not in body
    assert body["service_tier"] == "default"
    assert len(requests[0].headers["Idempotency-Key"]) == 64
    assert body["background"] is True
    assert constructors[0]["max_retries"] == 0
    assert usage.input_tokens == 50 and usage.output_tokens == 20
    if visual:
        import base64

        image_parts = [
            part
            for message in body["input"]
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part["type"] == "input_image"
        ]
        assert len(image_parts) == 1
        assert image_parts[0]["detail"] == "high"
        assert base64.b64decode(image_parts[0]["image_url"].split(",", 1)[1]) == images[0][0]


@pytest.mark.asyncio
async def test_large_evidence_and_usage_over_estimates_still_return_valid_result(monkeypatch):
    from types import SimpleNamespace

    class Responses:
        async def create(self, **request):
            assert len(request["input"][1]["content"][0]["text"]) > 128000
            data = _response('{"text":"accepted"}')
            data["usage"]["input_tokens"] = 200000
            data["usage"]["output_tokens"] = 20
            return Response.model_validate(data)

    monkeypatch.setattr(
        "apps.api.services.preparation_provider.make_chat_model",
        lambda **_: SimpleNamespace(root_async_client=SimpleNamespace(responses=Responses())),
    )
    parsed, usage = await invoke_preparation(
        frozen(), task().model_copy(update={"max_cost_microusd": 1}), Answer,
        instruction="read", evidence={"text": "source evidence " * 30000},
        idempotency_key="offline-large",
    )
    assert parsed.text == "accepted"
    assert usage.provider_called and usage.usage_known
    assert usage.input_tokens == 200000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        ("connection", "provider_unavailable"),
        ("internal", "turn_failed"),
        ("quota", "quota_exceeded"),
        ("configuration", "provider_configuration"),
    ],
)
async def test_invoke_preparation_classifies_sdk_and_internal_failures_safely(
    monkeypatch, kind, expected_code
):
    from apps.api.services import preparation_provider

    sentinel = "private prompt text and sk-provider-secret"
    request = httpx.Request("POST", "https://offline.invalid/v1/responses")
    if kind == "connection":
        failure = APIConnectionError(request=request)
        failure.args = (sentinel,)
    elif kind == "internal":
        failure = TypeError(sentinel)
    elif kind == "quota":
        body = {"error": {"code": "insufficient_quota", "message": sentinel}}
        response = httpx.Response(429, request=request, json=body)
        failure = RateLimitError(sentinel, response=response, body=body)
    else:
        response = httpx.Response(401, request=request, json={"error": sentinel})
        failure = AuthenticationError(sentinel, response=response, body={"error": sentinel})

    from types import SimpleNamespace

    class Responses:
        async def create(self, **_kwargs):
            raise failure

    monkeypatch.setattr(preparation_provider, "make_chat_model",
                        lambda **_: SimpleNamespace(root_async_client=SimpleNamespace(responses=Responses())))

    with pytest.raises(PreparationTaskFailure) as caught:
        await invoke_preparation(
            frozen(), task(), Answer,
            instruction="Return the assigned text.",
            evidence={"source": "offline"},
            idempotency_key="offline-failure-key",
        )

    assert caught.value.code == expected_code
    assert caught.value.usage.provider_called is True
    assert caught.value.usage.usage_known is False
    assert sentinel not in repr(caught.value)


def _response(text, *, status="completed", reason=None):
    return {
        "id": "resp_offline",
        "object": "response",
        "created_at": 1,
        "status": status,
        "model": "gpt-6.1-sol",
        "error": None,
        "incomplete_details": {"reason": reason} if reason else None,
        "instructions": None,
        "metadata": {},
        "output": [
            {
                "id": "msg_offline",
                "type": "message",
                "status": status,
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "tools": [],
        "tool_choice": "auto",
        "usage": {
            "input_tokens": 900,
            "output_tokens": 100,
            "total_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 90},
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "status", "reason"),
    [
        ('{"te', "incomplete", "max_output_tokens"),  # reasoning used the cap; answer truncated
        ('{"other": 1}', "completed", None),  # complete JSON that breaks the schema
    ],
)
async def test_completed_call_with_invalid_answer_is_a_known_retryable_failure(
    monkeypatch, text, status, reason
):
    """The raw response survives an invalid answer, so its usage is known and the task can retry."""
    from agent import llm

    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json=_response(text, status=status, reason=reason))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(llm, "OPENAI_API_KEY", "test-offline-never-network")
        monkeypatch.setattr(
            llm,
            "ChatOpenAI",
            lambda **kwargs: ChatOpenAI(
                **kwargs, http_async_client=client, base_url="https://offline.invalid/v1"
            ),
        )
        with pytest.raises(PreparationTaskFailure) as caught:
            await invoke_preparation(
                frozen(),
                task(),
                Answer,
                instruction="Return the assigned text.",
                evidence={"source": "untrusted"},
                idempotency_key="offline-key",
            )
    assert caught.value.code == "provider_output_invalid"
    usage = caught.value.usage
    assert usage.provider_called and usage.usage_known
    assert (usage.input_tokens, usage.output_tokens) == (900, 100)
    assert usage.failure_code == "provider_output_invalid"  # retryable for the supervisor
    text_format = json.loads(requests[0].content)["text"]["format"]
    assert text_format["type"] == "json_schema" and text_format["strict"] is True
    assert text_format["name"] == "Answer"
    assert text_format["schema"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_background_job_survives_worker_interruption_and_temporary_poll_failure(monkeypatch):
    import asyncio

    from agent import llm

    requests = []
    polling = asyncio.Event()
    poll_failure = True
    finish = False

    async def handle(request):
        nonlocal poll_failure
        requests.append(request)
        if request.method == "POST":
            data = _response("")
            data.update(status="queued", output=[], usage=None)
            return httpx.Response(200, json=data)
        polling.set()
        if poll_failure:
            poll_failure = False
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        data = _response('{"text":"recovered"}')
        if not finish:
            data.update(status="in_progress", output=[], usage=None)
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(llm, "OPENAI_API_KEY", "test-offline-never-network")
        monkeypatch.setattr(llm, "ChatOpenAI", lambda **kwargs: ChatOpenAI(
            **kwargs, http_async_client=client, base_url="https://offline.invalid/v1"))

        async def invoke(attempt):
            return await invoke_preparation(frozen(), task(), Answer, instruction="Return text.",
                                            evidence={"source": "saved", "attempt": attempt},
                                            idempotency_key=f"attempt-{attempt}")

        worker = asyncio.create_task(invoke(1))
        await asyncio.wait_for(polling.wait(), timeout=5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        finish = True
        answer, usage = await invoke(2)
    assert answer.text == "recovered"
    assert usage.usage_known and usage.call_count == 1
    assert len([r for r in requests if r.method == "POST"]) == 1
    assert all(r.url.path == "/v1/responses/resp_offline" for r in requests if r.method == "GET")
