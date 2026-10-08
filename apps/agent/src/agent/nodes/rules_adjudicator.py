"""Resolve the player's action: the engine decides the numbers, the LLM describes consequences.

Flow:
  1. `agent.resolution.resolve_from_parsed` turns the parser's proposed `check`/`difficulty`
     into a modifier from the character sheet, a DC, a d20 roll from `tools.dice`, and a
     deterministic success/failure (legacy `dice_expression` is tolerated; invalid dice are
     dropped, never fatal). The combat subgraph passes its own pre-rolled attack instead.
  2. The LLM is told the known outcome and asked only for consequences/side effects (it also
     sees world flags, the current scene and the story-so-far summary).
  3. The engine stamps the outcome over the LLM's answer and `enforce_side_effects` keeps only
     the side effects it can justify; `decide_completion` turns any "campaign_complete" flag
     into a proposal that needs confirmation.

Structured-output failure is retried once, then the turn continues with a neutral outcome (the
engine result, no side effects). Only the *rules* bucket goes into the prompt; adventure text is
narrator-only so the adjudicator cannot rule on flavour (it does get scene ids for `scene_id`).
"""

from __future__ import annotations

import logging

from agent.llm import make_chat_model
from agent.models.adjudication import AdjudicationResult
from agent.prompts import RULES_ADJUDICATOR, wrap_player_action
from agent.resolution import (
    CheckResult,
    decide_completion,
    enforce_side_effects,
    known_scene_ids,
    resolve_from_parsed,
)
from agent.state import AgentState

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_MAX_FLAGS_IN_PROMPT = 40


def _rules_context(state: AgentState) -> list:
    rules_context = state.get("rules_context", [])
    if not rules_context:
        rules_context = [c for c in state.get("retrieved_context", []) if c.get("kind") == "rules"]
    return rules_context


def build_adjudicator_prompt(
    state: AgentState,
    check_result: CheckResult | None,
    *,
    extra_outcome: str | None = None,
    dice_log: list[dict] | None = None,
) -> list[dict]:
    player_input = state.get("player_input", "")
    parsed = state.get("parsed_input")
    parts: list[str] = [wrap_player_action(player_input)]

    if check_result is not None:
        parts.append(f"Engine outcome (final, do not change): {check_result.describe()}")
    else:
        parts.append("Engine outcome (final): no roll needed; the attempt simply happens (success=true).")
    if extra_outcome:
        parts.append(f"Additional engine results this turn: {extra_outcome}")
    for entry in dice_log or []:
        parts.append(f"Other roll: {entry.get('expression')} = {entry.get('total')}")
    if parsed is not None and getattr(parsed, "target", None):
        parts.append(f"Target: {parsed.target}")

    character = state.get("character")
    if character:
        parts.append(
            f"Player character: {character.get('name')}, level {character.get('level')} "
            f"{character.get('class')}, HP {character.get('hp_current')}/{character.get('hp_max')}, "
            f"stats {character.get('stats')}, inventory {character.get('inventory')}"
        )

    scene = state.get("current_scene_id")
    parts.append(f"Current scene id: {scene or '(none yet)'}")
    known = sorted(known_scene_ids(scene, state.get("adventure_context")) - {scene})
    if known:
        parts.append(f"Scene ids seen in the adventure context: {', '.join(known[:20])}")

    flags = state.get("world_flags") or {}
    if flags:
        shown = list(flags.items())[:_MAX_FLAGS_IN_PROMPT]
        parts.append("Current world flags (key = value): " + "; ".join(f"{k} = {v}" for k, v in shown))
    else:
        parts.append("Current world flags: (none)")

    summary = state.get("memory_summary")
    if summary:
        parts.append(f"Story so far: {summary}")

    rules_context = _rules_context(state)
    if rules_context:
        parts.append("Official rules excerpts (Player Handbook, DMG, Monster Manual):")
        for c in rules_context:
            parts.append(f"- {c.get('text', '')} (source: {c.get('source', '')})")

    return [
        {"role": "system", "content": RULES_ADJUDICATOR},
        {"role": "user", "content": "\n".join(parts)},
    ]


async def _invoke_with_retry(messages: list[dict]) -> AdjudicationResult | None:
    llm = make_chat_model(temperature=0)
    structured_llm = llm.with_structured_output(AdjudicationResult, method="function_calling")
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            result = await structured_llm.ainvoke(messages)
            if not isinstance(result, AdjudicationResult):
                result = AdjudicationResult.model_validate(result)
            return result
        except Exception as e:
            log.warning(
                "adjudicator_structured_output_failed",
                extra={"attempt": attempt, "error": str(e)[:200]},
            )
    return None


def _neutral(check_result: CheckResult | None, state: AgentState) -> AdjudicationResult:
    if check_result is not None:
        summary = check_result.describe()
    else:
        summary = "The attempt proceeds without notable consequence."
    return AdjudicationResult(success=True, mechanical_summary=summary)


async def adjudicate(
    state: AgentState,
    *,
    engine_check: CheckResult | None = None,
    in_combat: bool = False,
    extra_outcome: str | None = None,
) -> AdjudicationResult:
    """Engine-first adjudication shared by the linear path and the combat subgraph.

    In combat, the caller passes its own attack/check (`engine_check`), owns HP, and decides
    campaign completion after damage is known; outside combat this resolves the parser's check.
    """
    parsed = state.get("parsed_input")
    character = state.get("character")
    player_input = state.get("player_input", "")

    check_result = engine_check
    extra_log: list[dict] = []
    if check_result is None and not in_combat:
        check_result, extra_log = resolve_from_parsed(character, parsed, player_input=player_input)

    messages = build_adjudicator_prompt(
        state, check_result, extra_outcome=extra_outcome, dice_log=extra_log
    )
    result = await _invoke_with_retry(messages)
    if result is None:
        result = _neutral(check_result, state)

    # Authoritative numbers: never trust the model's success/dice.
    result.dice_log = list(extra_log)
    if check_result is not None:
        check_result.apply_to(result)
        if check_result.describe() not in result.mechanical_summary:
            result.mechanical_summary = f"{check_result.describe()}. {result.mechanical_summary}".strip()
    else:
        result.success = True
        result.dice_result = extra_log[0]["total"] if extra_log else None

    enforce_side_effects(
        result,
        character=character,
        check_result=check_result,
        world_flags=state.get("world_flags"),
        current_scene_id=state.get("current_scene_id"),
        adventure_context=state.get("adventure_context"),
        player_input=player_input,
        parsed=parsed,
        in_combat=in_combat,
    )
    if not in_combat:
        decide_completion(
            result,
            world_flags=state.get("world_flags"),
            parsed=parsed,
            player_input=player_input,
        )
    return result


async def rules_adjudicator_node(state: AgentState) -> dict:
    result = await adjudicate(state)
    return {"adjudication_result": result}
