from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage


def _write_profiles(path: Path, *, parser_model: str, narrator_model: str) -> None:
    path.write_text(
        'profile_version = "lifecycle"\n'
        "[profiles.play.parser]\n"
        f"model = '{parser_model}'\ntemperature = 0.2\nmax_output_tokens = 101\n"
        "[profiles.play.narrator]\n"
        f"model = '{narrator_model}'\ntemperature = 0.3\nmax_output_tokens = 202\n",
        encoding="utf-8",
    )


class _ProviderSpy:
    def __init__(self, calls: list[dict], **kwargs) -> None:
        self.calls = calls
        calls.append(kwargs)

    def with_structured_output(self, _schema, **_kwargs):
        class _Structured:
            async def ainvoke(self, *_args, **_kwargs):
                from agent.models.parsed_input import ParsedInput

                return ParsedInput(intent="exploration")

        return _Structured()

    async def astream(self, *_args, **_kwargs) -> AsyncIterator[AIMessage]:
        yield AIMessage(content="The path remains quiet.")


@pytest.mark.asyncio
async def test_sync_turn_freezes_profile_file_until_graph_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agent.llm as llm
    from agent.nodes import input_parser
    from apps.api.services import turn_runner
    from edgar_core.model_profiles import current_model_profiles

    path = tmp_path / "profiles.toml"
    _write_profiles(path, parser_model="gpt-4.1", narrator_model="gpt-4.1")
    monkeypatch.setenv("MODEL_PROFILES_PATH", str(path))
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(input_parser, "OPENAI_API_KEY", "test-key-for-edg-141")
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "test-key-for-edg-141")
    provider_calls: list[dict] = []
    monkeypatch.setattr(
        llm, "ChatOpenAI", lambda **kwargs: _ProviderSpy(provider_calls, **kwargs)
    )

    class FakeApp:
        async def ainvoke(self, _initial_state):
            first = await input_parser.input_parser_node({"player_input": "I wait."})
            assert first["parsed_input"].intent == "exploration"
            _write_profiles(path, parser_model="gpt-4o", narrator_model="gpt-4o")
            await input_parser.input_parser_node({"player_input": "I listen."})
            return {"ok": True}

    monkeypatch.setattr(turn_runner, "app", FakeApp())
    assert await turn_runner.run_turn({"player_input": "I wait."}) == {"ok": True}
    assert [call["model"] for call in provider_calls] == ["gpt-4.1", "gpt-4.1"]
    assert provider_calls[0]["api_key"] == "test-key-for-edg-141"
    assert current_model_profiles().profiles["play.parser"].model == "gpt-4o"


@pytest.mark.asyncio
async def test_sse_turn_uses_same_frozen_profiles_for_parser_and_streamed_narrator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agent.llm as llm
    from agent.nodes import input_parser
    from apps.api.services import turn_runner

    path = tmp_path / "profiles.toml"
    _write_profiles(path, parser_model="gpt-4.1", narrator_model="gpt-4.1")
    monkeypatch.setenv("MODEL_PROFILES_PATH", str(path))
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(input_parser, "OPENAI_API_KEY", "test-key-for-edg-141")
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "test-key-for-edg-141")
    provider_calls: list[dict] = []
    monkeypatch.setattr(
        llm, "ChatOpenAI", lambda **kwargs: _ProviderSpy(provider_calls, **kwargs)
    )

    class FakePrepareApp:
        async def astream(self, _initial_state, **_kwargs):
            parsed = await input_parser.input_parser_node({"player_input": "I wait."})
            yield "updates", {"input_parser": parsed}
            _write_profiles(path, parser_model="gpt-4o", narrator_model="gpt-4o")
            yield "values", {"player_input": "I wait.", "adjudication_result": None}

    monkeypatch.setattr(turn_runner, "prepare_app", FakePrepareApp())
    monkeypatch.setattr(turn_runner, "deterministic_narration", lambda _state: None)
    monkeypatch.setattr(turn_runner, "actions_for_state", lambda _state: [])
    monkeypatch.setattr(turn_runner, "build_narrator_prompt", lambda _state: [])
    monkeypatch.setattr(turn_runner, "persist_turn", lambda *_args: _done())

    events = [
        frame
        async for frame in turn_runner.stream_session_turn(
            session_id=17,
            body_message="I wait.",
            initial_state={"player_input": "I wait."},
        )
    ]

    assert provider_calls[0]["model"] == "gpt-4.1"
    assert provider_calls[0]["temperature"] == 0.2
    assert provider_calls[0]["max_tokens"] == 101
    assert provider_calls[1]["model"] == "gpt-4.1"
    assert provider_calls[1]["temperature"] == 0.3
    assert provider_calls[1]["max_tokens"] == 202
    assert provider_calls[0]["api_key"] == provider_calls[1]["api_key"] == "test-key-for-edg-141"
    assert any(event.startswith("event: done\n") for event in events)


async def _done() -> dict:
    return {"event_id": 1, "narration": "The path remains quiet."}
