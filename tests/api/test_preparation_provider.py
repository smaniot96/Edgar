"""Exercise the real LangChain/OpenAI request encoding with an in-memory HTTP transport."""

import json

import httpx
import pytest
from langchain_openai import ChatOpenAI
from pydantic import BaseModel
from test_preparation_supervisor import binding

from apps.api.services.preparation import PreparationTaskFailure
from apps.api.services.preparation_provider import invoke_preparation, request_cost_ceiling
from edgar_core.model_profiles import load_model_profiles
from edgar_core.preparation import PreparationStage, PreparationTask, canonical_sha256


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
    assert requests[0].headers["Idempotency-Key"] == "offline-key"
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
async def test_oversized_evidence_fails_before_provider_with_known_zero_usage(monkeypatch):
    monkeypatch.setattr(
        "apps.api.services.preparation_provider.make_chat_model",
        lambda **_: pytest.fail("predispatch must not construct provider"),
    )
    with pytest.raises(PreparationTaskFailure) as error:
        await invoke_preparation(
            frozen(),
            task(),
            Answer,
            instruction="read",
            evidence={"text": "a" * 30000},
            idempotency_key="unused",
        )
    assert error.value.code == "task_context_limit"
    assert error.value.usage.provider_called is False
    assert error.value.usage.usage_known
