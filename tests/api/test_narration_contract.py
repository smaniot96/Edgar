"""Developed narration shares one grounded prompt and outcome across sync/SSE.

The provider prose is scripted: these tests check transport and source boundaries, not quality.
Only the already-adjudicated state is supplied to the SSE graph stand-in.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from agent.models.adjudication import AdjudicationResult
from agent.nodes.narrator import narrator_node
from apps.api.services import turn_runner
from tests.agent.test_story_effects import fixture_state
from tests.api.test_combat_stream_contract import committed, done_of, tokens_of  # noqa: F401
from tests.api.test_turn_pipeline import _sse_events


@pytest.mark.parametrize("responses_blocks", [False, True])
async def test_sync_and_stream_keep_grounded_paragraphs_and_the_failed_ruling(
    monkeypatch,
    committed,  # noqa: F811 - imported pytest fixture
    responses_blocks,
):
    state, public, private = fixture_state()
    state.update(
        player_input="I ask the ferryman to leave his post and search for the boat.",
        adventure_context=[
            {**public, "text": "Wet stone steps lead to the quay. The ferryman holds the rope."},
            private,
        ],
        adjudication_result=AdjudicationResult(
            success=False,
            check="persuasion",
            dc=15,
            roll_expression="1d20+1",
            dice_result=8,
            mechanical_summary="The ferryman refuses to leave his post.",
            accepted_observations=["The boat was due before sunset."],
        ),
    )
    original = deepcopy(state)
    chunks = [
        "The ferryman keeps one hand on the rope. ",
        "'The boat was due before sunset,' he says, but refuses to leave his post.",
        "\n",
        "\nThe wet stone steps lead down to the quay. ",
        "His answer leaves the boat's whereabouts unresolved.",
        "\n\nWhat do you ask him next?",
    ]
    prompts = []
    calls = []

    def content(text):
        return [{"type": "text", "text": text}] if responses_blocks else text

    async def invoke(prompt):
        calls.append("ainvoke")
        prompts.append(prompt)
        return AIMessage(content=content("".join(chunks)))

    async def stream(prompt):
        calls.append("astream")
        prompts.append(prompt)
        for text in chunks:
            yield AIMessage(content=content(text))

    model = SimpleNamespace(ainvoke=invoke, astream=stream)
    monkeypatch.setattr("agent.nodes.narrator.make_narrator_model", lambda *_: model)
    monkeypatch.setattr(turn_runner, "make_narrator_model", lambda *_: model)

    async def prepared_turn(*_args, **_kwargs):
        yield "values", deepcopy(state)

    monkeypatch.setattr(turn_runner, "prepare_app", SimpleNamespace(astream=prepared_turn))

    sync = await narrator_node(state)
    frames = [
        frame
        async for frame in turn_runner.stream_session_turn(
            session_id=1, body_message=state["player_input"], initial_state=state
        )
    ]
    events = _sse_events("".join(frames))

    assert calls == ["ainvoke", "astream"]
    assert prompts[0] == prompts[1]
    sent = "\n".join(message.content for message in prompts[0])
    assert private["text"] not in sent and "Secret future finale" not in sent
    assert "The boat was due before sunset." in sent
    assert "1d20+1 = 8 vs DC 15 -> failure" in sent
    assert tokens_of(events) == done_of(events)["narration"] == sync["narration"] == "".join(chunks)
    assert len(committed) == 1
    assert committed[0]["adjudication_result"] == original["adjudication_result"]
    assert state == original
