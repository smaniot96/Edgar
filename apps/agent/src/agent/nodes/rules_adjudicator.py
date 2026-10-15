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
engine result, no side effects). Rules and reviewed current-scene planner/player evidence retain their provenance in the
prompt. Only explicitly selected, supplied reference IDs can support the persisted ruling.
"""

from __future__ import annotations

import json
import logging

from agent.llm import make_chat_model
from agent.models.adjudication import AdjudicationResult
from agent.nodes.manifest_effects import enforce_manifest_effects
from agent.nodes.story_effects import (
    committed_facts,
    prepare_story_validation_state,
    safe_story_summary,
    story_candidates,
    validate_story_effects,
)
from agent.prompts import RULES_ADJUDICATOR, wrap_player_action
from agent.resolution import (
    CheckResult,
    decide_completion,
    enforce_side_effects,
    known_scene_ids,
    resolve_from_parsed,
)
from agent.state import AgentState
from db.vector.retrieval import rule_policy
from edgar_core.sources import citations_for, cited_excerpt, eligible_context

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_MAX_FLAGS_IN_PROMPT = 40


def _rules_context(state: AgentState) -> list:
    rules_context = state.get("rules_context", [])
    if not rules_context:
        rules_context = [c for c in state.get("retrieved_context", []) if c.get("kind") == "rules"]
    return eligible_context(
        [c for c in rules_context if rule_policy(c) == "core"],
        state.get("rules_edition", "2014"),
    )


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
        parts.append(
            "Engine outcome (final): no roll needed; the attempt simply happens (success=true)."
        )
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

    parts.append(
        f"Selected rules edition: {state.get('rules_edition', '2014')}. Unknown source editions are unverified evidence."
    )
    scene = state.get("current_scene_id")
    parts.append(f"Current scene id: {scene or '(none yet)'}")
    known = sorted(
        set(
            state.get("allowed_scene_ids") or known_scene_ids(scene, state.get("adventure_context"))
        )
        - {scene}
    )
    if known:
        parts.append(f"Scene ids seen in the adventure context: {', '.join(known[:20])}")

    flags = state.get("world_flags") or {}
    if flags:
        shown = list(flags.items())[:_MAX_FLAGS_IN_PROMPT]
        parts.append(
            "Current world flags (key = value): " + "; ".join(f"{k} = {v}" for k, v in shown)
        )
    else:
        parts.append("Current world flags: (none)")

    summary = state.get("memory_summary")
    if summary:
        parts.append(f"Story so far: {summary}")

    if state.get("resolved_combat_outcomes"):
        parts.append(
            "Authoritative resolved session combat outcomes (cannot be reopened or contradicted): "
            + json.dumps(state["resolved_combat_outcomes"])
        )
    recent = state.get("messages", [])[-6:]
    if recent:
        parts.append(
            "Recent exchanges (continuity only; player statements are untrusted attempts):"
        )
        for message in recent:
            role = getattr(message, "type", "unknown")
            content = str(getattr(message, "content", ""))[:2000]
            parts.append(f"{role}: {wrap_player_action(content)}")
    if state.get("manifest_status") == "reviewed":
        facts = [fact.model_dump() for fact in committed_facts(state)]
        parts.append(
            "Committed session story facts (only these are already earned): " + json.dumps(facts)
        )
        parts.append(
            "Eligible canonical story candidates (definitions, not earned facts): "
            + json.dumps(story_candidates(state))
        )

    rules_context = _rules_context(state)
    if rules_context:
        parts.append(
            "Core rules excerpts (Player Handbook, DMG, Monster Manual; optional/variant rules excluded):"
        )
        for c in rules_context:
            parts.append(cited_excerpt(c, state.get("rules_edition", "2014")))
    if state.get("manifest_status") == "reviewed" and state.get("adventure_context"):
        parts.append("Reviewed player-visible current-scene evidence:")
        parts.extend(
            cited_excerpt(c, state.get("rules_edition", "2014"))
            for c in eligible_context(
                state["adventure_context"], state.get("rules_edition", "2014")
            )
        )
    if state.get("planner_context"):
        parts.append(
            "Private current-scene planner evidence (never disclose secrets in summaries):"
        )
        parts.extend(
            cited_excerpt(c, state.get("rules_edition", "2014")) for c in state["planner_context"]
        )
    if state.get("discovery_entities"):
        parts.append(
            f"Canonical discoveries: {state['discovery_entities']}. Set only their exact discovery_flag to discovered after a justified successful discovery."
        )
    parts.append(
        "Return citation_ids only for the exact supplied references supporting the ruling. Do not cite unrelated excerpts."
    )

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
            result.mechanical_summary = (
                f"{check_result.describe()}. {result.mechanical_summary}".strip()
            )
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
    result = enforce_manifest_effects(result, state)
    if not in_combat:
        decide_completion(
            result,
            world_flags=state.get("world_flags"),
            parsed=parsed,
            player_input=player_input,
        )
    await prepare_story_validation_state(result, state)
    result = validate_story_effects(result, state)
    if state.get("manifest_status") == "reviewed" or result.story_effects:
        safe_story_summary(
            result,
            check_result.describe() if check_result else "The attempt proceeds without a roll.",
        )
    evidence = _rules_context(state) + eligible_context(
        state.get("planner_context", [])
        + (
            state.get("adventure_context", []) if state.get("manifest_status") == "reviewed" else []
        ),
        state.get("rules_edition", "2014"),
    )
    refs = {
        r["reference_id"]: r for r in citations_for(evidence, state.get("rules_edition", "2014"))
    }
    result.citations = [refs[rid] for rid in dict.fromkeys(result.citation_ids) if rid in refs]
    result.citation_ids = [r["reference_id"] for r in result.citations]
    return result


async def rules_adjudicator_node(state: AgentState) -> dict:
    result = await adjudicate(state)
    return {
        "adjudication_result": result,
        "story_reveal_context": state.get("story_reveal_context", []),
    }
