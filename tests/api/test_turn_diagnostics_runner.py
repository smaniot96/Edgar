"""Turn runners attach one `turn_diagnostics` row to the turn (EDG-053). No database, fake models only.

The rows are inspected where `persist_turn` receives them, so the same staging that commits with the
turn in Postgres is exercised here without a connection. Postgres-backed persistence and rollback are
covered in tests/api/test_turn_diagnostics_api.py (hosted CI).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agent.combat_telemetry import COMBAT_ROUND_EVENT
from agent.models.parsed_input import ParsedInput
from agent.provider_trace import traced_call
from agent.turn_diagnostics import TURN_DIAGNOSTICS_EVENT
from apps.api.services import turn_runner


def _diagnostics_rows(events: list[dict]) -> list[dict]:
    return [e["payload"] for e in events if e.get("event_type") == TURN_DIAGNOSTICS_EVENT]


class _SessionCounter:
    """Stands in for the session factory: counts how many sessions are open right now."""

    def __init__(self) -> None:
        self.open = 0

    def __call__(self):
        counter = self

        class _Session:
            async def __aenter__(self):
                counter.open += 1
                return object()

            async def __aexit__(self, *_exc):
                counter.open -= 1

        return _Session()


class _DelayedStreamingNarrator:
    """Streams two text chunks and a final usage chunk after a delay; records open sessions."""

    def __init__(self, counter: _SessionCounter, observed: list[int]) -> None:
        self._counter = counter
        self._observed = observed

    async def astream(self, _prompt):
        await asyncio.sleep(0.01)  # The provider is "waiting": no DB connection may be held.
        self._observed.append(self._counter.open)
        yield AIMessageChunk(content="The door ")
        await asyncio.sleep(0.01)
        self._observed.append(self._counter.open)
        yield AIMessageChunk(content="creaks.")
        yield AIMessageChunk(
            content="",
            usage_metadata={"input_tokens": 40, "output_tokens": 9, "total_tokens": 49},
            response_metadata={"model_name": "gpt-6.1-sol"},
        )


def _done(persisted: dict):
    async def persist(_session_id, _message, result, **_spotlight):
        persisted["result"] = result
        return {"narration": result.get("narration", "")}

    return persist


@pytest.fixture
def stream_env(monkeypatch):
    counter = _SessionCounter()
    import db.postgres.session as db_session_module

    monkeypatch.setattr(db_session_module, "async_session_factory", counter)
    monkeypatch.setattr(turn_runner, "deterministic_narration", lambda _state: None)
    monkeypatch.setattr(turn_runner, "actions_for_state", lambda _state: [])
    monkeypatch.setattr(turn_runner, "build_narrator_prompt", lambda _state: [])
    observed: list[int] = []
    monkeypatch.setattr(
        turn_runner,
        "make_narrator_model",
        lambda: _DelayedStreamingNarrator(counter, observed),
    )
    return counter, observed


def _prepare_app(*steps):
    class FakePrepareApp:
        async def astream(self, _initial_state, **_kwargs):
            for step in steps:
                yield step

    return FakePrepareApp()


async def _stream(**kwargs) -> list[str]:
    return [
        frame
        async for frame in turn_runner.stream_session_turn(
            session_id=3,
            body_message="I look.",
            initial_state={"player_input": "I look.", "turn_id": "turn-7"},
            **kwargs,
        )
    ]


@pytest.mark.asyncio
async def test_stream_turn_holds_no_session_during_the_provider_wait(
    monkeypatch, stream_env
) -> None:
    from agent import provider_trace

    # Pin the requested model so the cost assertion does not depend on local profile files.
    monkeypatch.setattr(provider_trace, "_requested_model", lambda _profile: "gpt-6.1-sol")
    counter, observed = stream_env
    persisted: dict = {}
    values = {"player_input": "I look.", "adjudication_result": None, "turn_id": "turn-7"}
    monkeypatch.setattr(
        turn_runner,
        "prepare_app",
        _prepare_app(
            ("updates", {"input_parser": {"parsed_input": ParsedInput(intent="exploration")}}),
            ("values", values),
        ),
    )
    monkeypatch.setattr(turn_runner, "persist_turn", _done(persisted))

    frames = await _stream()

    assert observed == [0, 0], "a DB session was open while the provider was streaming"
    assert counter.open == 0
    assert any(f.startswith("event: done\n") for f in frames)
    (row,) = _diagnostics_rows(persisted["result"]["telemetry_events"])
    assert row["delivery"] == "stream"
    assert row["path"] == "normal"
    assert row["turn_id"] == "turn-7"
    assert row["time_to_first_text_ms"] is not None
    assert "narrating_ms" in row["stages_ms"]
    (call,) = row["provider_calls"]
    assert (call["kind"], call["streamed"]) == ("narrate", True)
    assert call["usage"] == {"input_tokens": 40, "output_tokens": 9, "total_tokens": 49}
    assert call["cost_microusd"] == 40 * 4 + 9 * 20


@pytest.mark.asyncio
async def test_stream_combat_turn_adopts_the_round_timings_under_shared_names(
    monkeypatch, stream_env
) -> None:
    persisted: dict = {}
    round_row = {
        "event_type": COMBAT_ROUND_EVENT,
        "payload": {
            "turn_id": "turn-8",
            "timings_ms": {"retrieving_ms": 11, "adjudicating_ms": 22, "narrating_ms": 33},
            "timings_unavailable": [],
        },
    }
    values = {
        "player_input": "I attack.",
        "narration": "The goblin falls.",
        "telemetry_events": [round_row],
    }
    monkeypatch.setattr(
        turn_runner,
        "prepare_app",
        _prepare_app(
            ("updates", {"input_parser": {"parsed_input": ParsedInput(intent="combat")}}),
            ("values", values),
        ),
    )
    monkeypatch.setattr(turn_runner, "persist_turn", _done(persisted))

    await _stream()

    events = persisted["result"]["telemetry_events"]
    (row,) = _diagnostics_rows(events)
    assert row["path"] == "combat"
    assert row["stages_ms"] == {"retrieving_ms": 11, "adjudicating_ms": 22, "narrating_ms": 33}
    # The combat round row itself is still staged for the developer view, untouched.
    assert any(e["event_type"] == COMBAT_ROUND_EVENT for e in events)


@pytest.mark.asyncio
async def test_sync_turn_appends_one_row_with_the_provider_call_it_made(monkeypatch) -> None:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    class _Llm(FakeListChatModel):
        """A fake chat model that reports usage through LangChain's own message field."""

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            from langchain_core.outputs import ChatGeneration, ChatResult

            message = AIMessage(
                content="A quiet room.",
                usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                response_metadata={"model_name": "gpt-unpriced-test"},
            )
            return ChatResult(generations=[ChatGeneration(message=message)])

    llm = _Llm(responses=["unused"])

    class FakeApp:
        async def ainvoke(self, _initial_state):
            reply = await traced_call(
                lambda: llm.ainvoke("describe"), kind="narrate", profile="play.narrator"
            )
            return {"narration": reply.content}

    monkeypatch.setattr(turn_runner, "app", FakeApp())

    result = await turn_runner.run_turn({"player_input": "I look.", "turn_id": "t-sync"})

    assert result["narration"] == "A quiet room."
    (row,) = _diagnostics_rows(result["telemetry_events"])
    assert row["delivery"] == "sync"
    assert row["path"] == "normal"
    assert row["turn_id"] == "t-sync"
    assert row["lock"] is None
    (call,) = row["provider_calls"]
    assert call["usage"] == {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}
    assert call["resolved_model"] == "gpt-unpriced-test"
    assert call["cost_microusd"] is None  # Usage known, no configured price: never guessed.
    assert row["totals"]["complete"] is False


@pytest.mark.asyncio
async def test_sync_row_carries_the_lock_wait_the_router_measured(monkeypatch) -> None:
    class FakeApp:
        async def ainvoke(self, _initial_state):
            return {"narration": "ok"}

    monkeypatch.setattr(turn_runner, "app", FakeApp())
    diagnostics = turn_runner.new_turn_diagnostics(
        turn_id="t-lock", delivery="sync", lock_wait_ms=2
    )

    result = await turn_runner.run_turn({"player_input": "wait"}, diagnostics=diagnostics)

    (row,) = _diagnostics_rows(result["telemetry_events"])
    assert row["lock"]["wait_ms"] == 2
    assert row["provider_calls"] == []  # No call made: an empty list, not an invented call.
    assert row["totals"]["calls"] == 0
    json.dumps(row)  # The stored row is plain JSON.


@pytest.mark.asyncio
async def test_failed_sync_turn_returns_no_row_because_nothing_is_persisted(monkeypatch) -> None:
    class FakeApp:
        async def ainvoke(self, _initial_state):
            return {"error": "boom", "error_code": "provider_unavailable"}

    monkeypatch.setattr(turn_runner, "app", FakeApp())
    with pytest.raises(turn_runner.TurnError):
        await turn_runner.run_turn({"player_input": "x"})
