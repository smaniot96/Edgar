"""LLM factory — monkeypatch `make_chat_model` in tests."""

from typing import Any

from langchain_openai import ChatOpenAI

from edgar_core.config import LLM_MODEL, LLM_REASONING_EFFORT, OPENAI_API_KEY

# Upper bound for one narration (~3 short paragraphs). Keeps latency/cost predictable and stops
# runaway "status block" style output. On reasoning models this cap also covers reasoning tokens,
# which is why they run at a low `reasoning_effort`.
NARRATOR_MAX_TOKENS = 600

# Model families that reject a non-default `temperature` and take `reasoning_effort` instead.
_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5", "gpt-6")


def is_reasoning_model(model: str) -> bool:
    return model.startswith(_REASONING_PREFIXES) and "chat" not in model


def chat_model_kwargs(model: str, temperature: float, max_tokens: int | None) -> dict:
    """Provider kwargs for `model`: temperature for classic models, reasoning effort otherwise.

    Reasoning models go through the Responses API: on Chat Completions, gpt-6-luna rejects
    function tools (our structured output) unless `reasoning_effort` is "none".
    """
    kwargs: dict = {"model": model}
    if is_reasoning_model(model):
        kwargs["reasoning_effort"] = LLM_REASONING_EFFORT
        kwargs["use_responses_api"] = True
    else:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return kwargs


def make_chat_model(
    temperature: float = 0,
    timeout: float = 60,
    max_retries: int = 3,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """Build a ChatOpenAI client for `LLM_MODEL`.

    `timeout` (seconds) and `max_retries` bound each request so a hung or
    rate-limited OpenAI call can't pin a turn forever. Both are overridable.
    `max_tokens` caps the completion length (None = provider default).
    `temperature` is only sent to models that accept it (see `is_reasoning_model`).
    """
    return ChatOpenAI(
        api_key=OPENAI_API_KEY,
        timeout=timeout,
        max_retries=max_retries,
        **chat_model_kwargs(LLM_MODEL, temperature, max_tokens),
    )


def message_text(message: Any) -> str:
    """Plain text of a chat response or stream chunk.

    Chat Completions returns `content` as a str; the Responses API returns a list of blocks
    (`[{"type": "text", "text": "..."}]`, plus reasoning blocks without text). Normalise both.
    """
    raw = message.content if hasattr(message, "content") else message
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(raw)
