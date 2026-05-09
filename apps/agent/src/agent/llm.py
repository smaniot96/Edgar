"""LLM factory — monkeypatch `make_chat_model` in tests."""

from edgar_core.config import LLM_MODEL, OPENAI_API_KEY
from langchain_openai import ChatOpenAI


def make_chat_model(temperature: float = 0) -> ChatOpenAI:
    return ChatOpenAI(
        model=LLM_MODEL,
        temperature=temperature,
        api_key=OPENAI_API_KEY,
    )
