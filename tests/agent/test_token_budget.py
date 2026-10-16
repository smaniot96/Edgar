"""Token budgets bound the continuity prompts while earned facts and outcomes stay intact."""

import asyncio
import logging
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.history import continuity_context, history_token_budget
from agent.models.parsed_input import ParsedInput
from agent.nodes.input_parser import _parser_context
from agent.nodes.narrator import build_narrator_prompt
from agent.nodes.rules_adjudicator import build_adjudicator_prompt
from agent.prompts import INPUT_PARSER, RULES_ADJUDICATOR
from edgar_core.model_profiles import (
    ModelProfileError,
    ModelProfileSnapshot,
    load_model_profiles,
    model_profile_snapshot,
    profile_diagnostics,
)
from edgar_core.token_budget import estimate_tokens, fit_count
from tests.agent.test_history_context import _state

PLAY_BUDGET_STAGES = (
    "play.parser",
    "play.consequence_planner",
    "play.narrator",
    "play.combat_narrator",
    "play.memory_summarizer",
)
CRITICAL = (
    "The lens tracks reflections, not the sky.",
    "South Weir field notes.pdf",
    "earned change 52",
    "victory",
    "The earlier summary begins the quay investigation.",
)


def limits_snapshot(tmp_path: Path, limits: dict[str, int]) -> ModelProfileSnapshot:
    body = "".join(
        f"[profiles.{stage}]\nmax_input_tokens = {limit}\n" for stage, limit in limits.items()
    )
    path = tmp_path / "profiles.toml"
    path.write_text(f"schema_version = 1\n{body}", encoding="utf-8")
    return load_model_profiles(path, environ={})


def all_stages(tmp_path: Path, limit: int) -> ModelProfileSnapshot:
    return limits_snapshot(tmp_path, dict.fromkeys(PLAY_BUDGET_STAGES, limit))


def long_turn_state() -> dict:
    """Fourteen turns of maximum-length player and narration text, then a recall question."""
    state = _state()
    messages = []
    for turn in range(1, 15):
        messages.extend(
            [
                HumanMessage(content=f"player action {turn} " + "x" * 2000),
                AIMessage(content=f"DM result {turn} " + "y" * 2400),
            ]
        )
    messages.extend(
        [
            HumanMessage(content="I ask Ilyra to remember the note."),
            AIMessage(content="Ilyra cannot remember the note."),
        ]
    )
    state["messages"] = messages
    return state


def prompts(state: dict) -> dict[str, str]:
    narrator = build_narrator_prompt(state)
    planner = build_adjudicator_prompt(state, None)
    return {
        "play.narrator": narrator[0].content + narrator[1].content,
        "play.consequence_planner": RULES_ADJUDICATOR + planner[1]["content"],
        "play.parser": INPUT_PARSER + _parser_context(state),
    }


def test_estimator_over_counts_prose_and_non_latin_text() -> None:
    prose = "The lens tracks reflections, not the sky. " * 20
    assert estimate_tokens(prose) >= len(prose) // 4
    assert estimate_tokens("月" * 30) >= 30
    assert estimate_tokens("") == 0
    assert fit_count([4, 4, 4], 8) == 2
    assert fit_count([4, 4, 4], 3) == 0
    assert fit_count([], 10) == 0


def test_long_turns_are_trimmed_to_the_input_budget_without_dropping_critical_facts(
    tmp_path: Path,
) -> None:
    state = long_turn_state()
    unbounded = prompts(state)
    assert all(estimate_tokens(text) > 26000 for text in unbounded.values())

    with model_profile_snapshot(all_stages(tmp_path, 26000)):
        bounded = prompts(state)

    for stage, text in bounded.items():
        assert estimate_tokens(text) <= 26000, stage
        assert "earlier verbatim messages omitted to fit the input budget" in text
        assert "player action 1 " not in text
        assert "Ilyra cannot remember the note." in text
        for critical in CRITICAL:
            assert critical in text, (stage, critical)


def test_degradation_drops_oldest_messages_first_and_keeps_a_contiguous_newest_tail() -> None:
    state = long_turn_state()
    full = continuity_context(state)
    fitted = continuity_context(state, estimate_tokens(full) // 2)

    kept = [m.content for m in state["messages"] if m.content in fitted]
    assert 0 < len(kept) < len(state["messages"])
    assert kept == [m.content for m in state["messages"][-len(kept) :]]
    assert fitted.startswith("Story so far (summary of earlier turns): ")
    assert f"[{len(state['messages']) - len(kept)} earlier verbatim messages omitted" in fitted


def test_fixed_content_over_budget_keeps_summary_and_facts_and_reports_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = long_turn_state()
    with (
        model_profile_snapshot(all_stages(tmp_path, 500)),
        caplog.at_level(logging.WARNING, logger="agent.history"),
    ):
        narrator = prompts(state)["play.narrator"]

    assert "Ilyra cannot remember the note." not in narrator
    for critical in CRITICAL:
        assert critical in narrator
    record = next(r for r in caplog.records if r.getMessage() == "continuity_trimmed")
    assert (record.stage, record.budget_tokens, record.over_budget) == ("play.narrator", 500, True)
    assert "Ilyra" not in str(record.__dict__)


def test_normal_length_turns_are_unchanged_by_the_default_budgets() -> None:
    state = _state()
    for text in prompts(state).values():
        assert continuity_context(state) in text
        assert "omitted to fit" not in text


def test_no_configured_budget_keeps_the_full_tail(tmp_path: Path) -> None:
    state = long_turn_state()
    with model_profile_snapshot(limits_snapshot(tmp_path, {})):
        assert history_token_budget() is None
        assert continuity_context(state) in prompts(state)["play.narrator"]


def test_history_budget_is_half_the_tightest_continuity_stage(tmp_path: Path) -> None:
    limits = {"play.parser": 40000, "play.consequence_planner": 30000, "play.narrator": 50000}
    with model_profile_snapshot(limits_snapshot(tmp_path, limits)):
        assert history_token_budget() == 15000


def test_budgets_are_inspectable_in_profile_diagnostics_without_text() -> None:
    profiles = profile_diagnostics(load_model_profiles())["profiles"]
    for stage in PLAY_BUDGET_STAGES:
        assert profiles[stage]["max_input_tokens"] == 32000
    assert profiles["play.session_intro"]["max_input_tokens"] is None


@pytest.mark.parametrize("value", ["0", "-5", "'big'", "true", "1.5"])
def test_invalid_input_budget_is_rejected(tmp_path: Path, value: str) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text(f"[profiles.play.narrator]\nmax_input_tokens = {value}\n", encoding="utf-8")
    with pytest.raises(ModelProfileError, match="max_input_tokens"):
        load_model_profiles(path, environ={})


def test_parser_uses_the_same_budgeted_context(tmp_path: Path, fake_llm_schema) -> None:
    from agent.nodes.input_parser import input_parser_node

    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="rp")
    with model_profile_snapshot(all_stages(tmp_path, 26000)):
        asyncio.run(input_parser_node(long_turn_state()))

    sent = fake_llm_schema.calls[-1][1]
    assert estimate_tokens(sent[0].content + sent[1].content) <= 26000
