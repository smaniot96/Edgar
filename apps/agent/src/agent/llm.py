"""LLM factory — monkeypatch `make_chat_model` in tests."""

from edgar_core.config import LLM_MODEL, OPENAI_API_KEY
from langchain_openai import ChatOpenAI


def make_chat_model(
    temperature: float = 0,
    timeout: float = 60,
    max_retries: int = 3,
) -> ChatOpenAI:
    """Build a ChatOpenAI client.

    `timeout` (seconds) and `max_retries` bound each request so a hung or
    rate-limited OpenAI call can't pin a turn forever. Both are overridable.
    """
    return ChatOpenAI(
        model=LLM_MODEL,
        temperature=temperature,
        api_key=OPENAI_API_KEY,
        timeout=timeout,
        max_retries=max_retries,
    )
