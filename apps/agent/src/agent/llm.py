"""LLM factory — monkeypatch `make_chat_model` in tests."""

from typing import Any

from langchain_openai import ChatOpenAI

from edgar_core.config import LLM_REASONING_EFFORT, OPENAI_API_KEY
from edgar_core.model_profiles import (
    ModelProfile,
    ModelProfileError,
    ModelProfileSnapshot,
    effective_profile,
    is_reasoning_model,
)

# Upper bound for one narration (~3 short paragraphs). Keeps latency/cost predictable and stops
# runaway "status block" style output. On reasoning models this cap also covers reasoning tokens,
# which is why they run at a low `reasoning_effort`.
NARRATOR_MAX_TOKENS = 600


def chat_model_kwargs(
    model: str,
    temperature: float,
    max_tokens: int | None,
    *,
    reasoning_effort: str | None = None,
    api: str = "auto",
) -> dict[str, Any]:
    """Provider kwargs for `model`: temperature for classic models, reasoning effort otherwise.

    Reasoning models go through the Responses API: on Chat Completions, gpt-6-luna rejects
    function tools (our structured output) unless `reasoning_effort` is "none".
    """
    kwargs: dict[str, Any] = {"model": model}
    if is_reasoning_model(model):
        kwargs["reasoning_effort"] = reasoning_effort or LLM_REASONING_EFFORT
        kwargs["use_responses_api"] = True
    else:
        kwargs["temperature"] = temperature
        if api == "responses":
            kwargs["use_responses_api"] = True
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return kwargs


def make_chat_model(
    temperature: float | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    max_tokens: int | None = None,
    *,
    profile: str | ModelProfile | None = None,
    profiles: ModelProfileSnapshot | None = None,
) -> ChatOpenAI:
    """Build a ChatOpenAI client for a named, validated stage profile.

    Configured profile values supply the defaults, while explicit call-site output limits are
    hard ceilings that may lower (but never raise) a profile's configured output cap. The
    remaining fallback arguments preserve per-call defaults when a profile leaves that setting
    empty. A supplied snapshot is the complete effective configuration for the request/run.
    """
    if isinstance(profile, ModelProfile):
        resolved = effective_profile(profile.name, profiles)
        if resolved != profile:
            raise ModelProfileError("A model profile must come from the selected frozen snapshot.")
    else:
        resolved = effective_profile(profile or "play.default", profiles)
    effective_temperature = (
        resolved.temperature if resolved.temperature is not None else temperature or 0
    )
    effective_max_tokens = resolved.max_output_tokens
    if max_tokens is not None:
        effective_max_tokens = (
            min(effective_max_tokens, max_tokens)
            if effective_max_tokens is not None
            else max_tokens
        )
    return ChatOpenAI(
        api_key=OPENAI_API_KEY,
        timeout=resolved.timeout_seconds if timeout is None else timeout,
        max_retries=resolved.retries if max_retries is None else max_retries,
        **chat_model_kwargs(
            resolved.model,
            effective_temperature,
            effective_max_tokens,
            reasoning_effort=resolved.reasoning_effort,
            api=resolved.api,
        ),
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
