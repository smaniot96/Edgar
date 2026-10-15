"""The three decision/narration prompts receive the same lossless uncovered history."""

from langchain_core.messages import AIMessage, HumanMessage

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.nodes.narrator import build_narrator_prompt
from agent.nodes.rules_adjudicator import build_adjudicator_prompt
from edgar_core.sources import source_citation
from edgar_core.story import StoryEffect, StoryFact, StoryRef, accept_story_effect


def _earned_clue() -> dict:
    text = "The lens tracks reflections, not the sky."
    source = {
        "source": "South Weir field notes.pdf",
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
        "manifest_sha256": "c" * 64,
        "collection": "south_weir",
        "rules_edition": "2014",
        "kind": "adventure",
        "page": 7,
        "passage_id": "lens_note",
        "scene_id": "quay",
        "audience": "player",
        "text": text,
    }
    citation = source_citation(source)
    target = StoryRef(
        collection="south_weir",
        source_sha256="a" * 64,
        manifest_sha256="c" * 64,
        kind="entity",
        canonical_id="lens_note",
    )
    accepted = accept_story_effect(
        StoryEffect(
            kind="discover",
            target=target,
            player_text=text,
            source_reference_ids=[citation["reference_id"]],
        ),
        turn_id="turn-seven",
        source_refs=[citation],
        display_name="Lens note",
    )
    fact = StoryFact(**accepted.model_dump(), fact_id="d" * 64, event_id=7)
    return {"facts": [fact.model_dump()]}


def _state() -> dict:
    messages = []
    for turn in range(1, 15):
        messages.extend(
            [
                HumanMessage(content=f"player action {turn}"),
                AIMessage(content=f"DM result {turn}"),
            ]
        )
    messages[-1] = AIMessage(content="The note text is exact: " + "LONG-VERBATIM " * 220)
    messages.extend(
        [
            HumanMessage(content="I ask Ilyra to remember the note."),
            AIMessage(content="Ilyra cannot remember the note."),
        ]
    )
    story_state = _earned_clue()
    original_fact = StoryFact.model_validate(story_state["facts"][0])
    story_state["facts"].extend(
        original_fact.model_copy(
            update={
                "fact_id": f"{turn:064x}",
                "event_id": turn,
                "player_text": f"earned change {turn}",
            }
        ).model_dump()
        for turn in range(8, 53)
    )
    return {
        "player_input": "I ask Ilyra what the note says.",
        "messages": messages,
        "memory_summary": "The earlier summary begins the quay investigation.",
        "story_state": story_state,
        "resolved_combat_outcomes": [{"id": 9, "outcome": "victory", "round": 4}],
        "manifest_status": "reviewed",
        "parsed_input": ParsedInput(intent="rp"),
        "adjudication_result": AdjudicationResult(success=True, mechanical_summary="Ilyra answers."),
    }


def test_parser_planner_and_narrator_get_all_uncovered_messages_and_durable_facts(
    fake_llm_schema,
):
    from agent.nodes.input_parser import input_parser_node

    state = _state()
    state["resolved_combat_outcomes"][0]["combatants"] = [
        {"source_reference": {"private_identity": "PRIVATE-COMBAT-SOURCE-BINDING"}}
    ]
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="rp")

    import asyncio

    asyncio.run(input_parser_node(state))
    assert "authoritative over conflicting older prose" in fake_llm_schema.calls[-1][1][0].content
    parser_text = fake_llm_schema.calls[-1][1][1].content
    planner_prompt = build_adjudicator_prompt(state, None)
    assert "authoritative over conflicting" in planner_prompt[0]["content"]
    planner_text = planner_prompt[1]["content"]
    narrator_prompt = build_narrator_prompt(state)
    narrator_text = narrator_prompt[-1].content
    assert "PRIVATE-COMBAT-SOURCE-BINDING" not in parser_text
    assert "PRIVATE-COMBAT-SOURCE-BINDING" not in narrator_text

    for prompt_text in (parser_text, planner_text, narrator_text):
        assert "player action 1" in prompt_text
        assert "player action 14" in prompt_text
        assert "Ilyra cannot remember the note." in prompt_text
        assert "LONG-VERBATIM " * 220 in prompt_text
        assert "The lens tracks reflections, not the sky." in prompt_text
        assert "South Weir field notes.pdf" in prompt_text
        assert "victory" in prompt_text
    assert "Committed player-known story facts" in narrator_text
    assert "Committed player-known story facts" in parser_text
    assert "Committed session story facts" in planner_text
    assert "earned change 52" in parser_text
    assert "earned change 52" in planner_text
    assert "earned change 52" in narrator_text
    assert "authoritative over conflicting older prose" in narrator_prompt[0].content
