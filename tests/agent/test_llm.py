"""Model kwargs: reasoning models get `reasoning_effort` and never a non-default temperature."""

import pytest

from agent.llm import chat_model_kwargs, is_reasoning_model


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-5.6-sol", "gpt-5-mini", "o3", "o4-mini"])
def test_reasoning_models_omit_temperature(model: str) -> None:
    kwargs = chat_model_kwargs(model, temperature=0.7, max_tokens=600)
    assert is_reasoning_model(model)
    assert "temperature" not in kwargs
    assert kwargs["reasoning_effort"]
    assert kwargs["max_tokens"] == 600


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4.1", "gpt-5-chat-latest"])
def test_classic_models_keep_temperature(model: str) -> None:
    kwargs = chat_model_kwargs(model, temperature=0.7, max_tokens=None)
    assert not is_reasoning_model(model)
    assert kwargs["temperature"] == 0.7
    assert "reasoning_effort" not in kwargs
    assert "max_tokens" not in kwargs


def test_reasoning_models_use_responses_api() -> None:
    assert chat_model_kwargs("gpt-6-luna", 0, None)["use_responses_api"] is True
    assert "use_responses_api" not in chat_model_kwargs("gpt-4o-mini", 0, None)


def test_message_text_handles_str_and_responses_blocks() -> None:
    from langchain_core.messages import AIMessage

    from agent.llm import message_text

    assert message_text(AIMessage(content="plain")) == "plain"
    blocks = [
        {"type": "reasoning", "summary": []},
        {"type": "text", "text": "The torch ", "annotations": []},
        {"type": "text", "text": "flares.", "annotations": []},
    ]
    assert message_text(AIMessage(content=blocks)) == "The torch flares."
