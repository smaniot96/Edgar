"""LLM factory — monkeypatch `make_chat_model` in tests."""

from langchain_openai import ChatOpenAI

from edgar_core.config import LLM_MODEL, OPENAI_API_KEY

# Upper bound for one narration (~3 short paragraphs). Keeps latency/cost predictable and stops
# runaway "status block" style output.
NARRATOR_MAX_TOKENS = 600


def make_chat_model(
    temperature: float = 0,
    timeout: float = 60,
    max_retries: int = 3,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """Build a ChatOpenAI client.

    `timeout` (seconds) and `max_retries` bound each request so a hung or
    rate-limited OpenAI call can't pin a turn forever. Both are overridable.
    `max_tokens` caps the completion length (None = provider default).
    """
    kwargs: dict = {}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(
        model=LLM_MODEL,
        temperature=temperature,
        api_key=OPENAI_API_KEY,
        timeout=timeout,
        max_retries=max_retries,
        **kwargs,
    )
