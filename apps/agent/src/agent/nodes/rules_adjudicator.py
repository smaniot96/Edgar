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
from dataclasses import replace

from agent.history import fit_continuity
from agent.llm import make_chat_model
from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.nodes.manifest_effects import enforce_manifest_effects
from agent.nodes.story_effects import (
    committed_facts,
    prepare_story_validation_state,
    safe_story_summary,
    story_candidates,
    validate_story_effects,
)
from agent.prepared_amendments import accept_runtime_amendment, amendment_prompt_block
from agent.prompts import RULES_ADJUDICATOR, wrap_player_action, wrap_retrieved_excerpt
from agent.provider_trace import traced_call
from agent.resolution import (
    CheckResult,
    decide_completion,
    enforce_side_effects,
    known_scene_ids,
    resolve_check,
    resolve_from_parsed,
)
from agent.resolution_policies import (
    describe_policy_result,
    exact_recall_fact,
    policy_effect_key,
    policy_effects_for_outcome,
    policy_source_is_fresh,
    selected_policy,
)
from agent.spells import NO_FOE_NOTE, SpellResult, cast_request, narrate, resolve_spell
from agent.state import AgentState
from agent.supported_rules import (
    ENGINE_ROLL_MODE_KEY,
    actions_for_state,
    apply_adjudication_boundary,
    boundary_prompt_block,
    engine_applied_for_state,
)
from db.vector.retrieval import rule_policy
from edgar_core.log_safety import error_summary
from edgar_core.sources import citations_for, cited_excerpt, eligible_context
from edgar_core.story import player_story_effect

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


_HOMEBREW_INSTRUCTIONS = (
    "Homebrew play (no adventure module). You may return homebrew_proposals, at most 3 per turn, "
    "for NEW places (kind place), named people (kind person), clues (kind clue) or threats (kind "
    "threat) consistent with the committed facts listed here. A place or person name must appear "
    "verbatim in its text; the text is the exact player-visible description. Never reuse, rename "
    "or contradict a committed name or fact. Never include dice, DCs, AC, HP, XP or other numbers, "
    "and never GM-only notes or secrets. Propose nothing after a failed check or for plain "
    "conversation and movement with nothing new to find. After a successful Insight, "
    "Investigation or Perception check, or when the player asks what they notice, propose at "
    "least one concrete clue or named detail that answers it."
)


def _homebrew_block(state: AgentState) -> str:
    facts = []
    for fact in committed_facts(state):
        public = player_story_effect(fact)
        if public is not None:
            facts.append(
                {
                    "kind": public["kind"],
                    "name": public["name"],
                    "text": public["text"],
                    "status": public["status"],
                }
            )
    return (
        "Committed session story facts (already earned; do not contradict or reuse their names): "
        + json.dumps(facts)
        + "\n"
        + _HOMEBREW_INSTRUCTIONS
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
    boundary = boundary_prompt_block(actions_for_state(state))
    if boundary:
        parts.append(boundary)

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

    continuity_slot = len(parts)

    if state.get("resolved_combat_outcomes"):
        parts.append(
            "Authoritative resolved session combat outcomes (cannot be reopened or contradicted): "
            + json.dumps(state["resolved_combat_outcomes"])
        )
    if state.get("manifest_status") == "reviewed":
        facts = [fact.model_dump() for fact in committed_facts(state)]
        parts.append(
            "Committed session story facts (only these are already earned): " + json.dumps(facts)
        )
        parts.append(
            "Eligible canonical story candidates (definitions, not earned facts): "
            + json.dumps(story_candidates(state))
        )

    if state.get("source_mode") == "homebrew" and state.get("manifest_status") == "homebrew":
        parts.append(_homebrew_block(state))

    rules_context = _rules_context(state)
    if rules_context:
        parts.append(
            "Core rules excerpts (Player Handbook, DMG, Monster Manual; optional/variant rules excluded):"
        )
        for c in rules_context:
            parts.append(
                wrap_retrieved_excerpt(cited_excerpt(c, state.get("rules_edition", "2014")))
            )
    if state.get("manifest_status") == "reviewed" and state.get("adventure_context"):
        parts.append("Reviewed player-visible current-scene evidence:")
        parts.extend(
            wrap_retrieved_excerpt(cited_excerpt(c, state.get("rules_edition", "2014")))
            for c in eligible_context(
                state["adventure_context"], state.get("rules_edition", "2014")
            )
        )
    if state.get("planner_context"):
        parts.append(
            "Private current-scene planner evidence (never disclose secrets in summaries):"
        )
        parts.extend(
            wrap_retrieved_excerpt(cited_excerpt(c, state.get("rules_edition", "2014")))
            for c in state["planner_context"]
        )
    if not (state.get("combat_state") or {}).get("ended") and not getattr(
        parsed, "resolution_policy_id", None
    ):
        amendment_context = amendment_prompt_block(state)
        if amendment_context:
            parts.append(amendment_context)
            parts.append(
                "For an uncovered plausible action only, return a proposed_prepared_amendment "
                "using the exact scene-bound structure above. A proposal is not accepted until "
                "the engine validates it. Do not tell the player about preparation or validation."
            )
    if state.get("discovery_entities"):
        parts.append(
            f"Canonical discoveries: {state['discovery_entities']}. Set only their exact discovery_flag to discovered after a justified successful discovery."
        )
    parts.append(
        "Return citation_ids only for the exact supplied references supporting the ruling. Do not cite unrelated excerpts."
    )

    # Budget the rendered separators too, including the one introduced by continuity insertion.
    # Many fixed blocks (such as the supported-mechanics contract) can otherwise overrun the cap.
    if continuity := fit_continuity(
        state, "play.consequence_planner", [RULES_ADJUDICATOR, "\n".join(parts) + "\n"]
    ):
        parts.insert(continuity_slot, continuity)
    return [
        {"role": "system", "content": RULES_ADJUDICATOR},
        {"role": "user", "content": "\n".join(parts)},
    ]


def _stamp_spell_outcome(
    result: AdjudicationResult, outcome: SpellResult, character: dict | None
) -> None:
    """Write the engine's spell effect over the model's answer: healing, slots and the line."""
    cu = result.character_update or CharacterUpdate()
    if outcome.healing:
        cu.hp_delta = (cu.hp_delta or 0) + outcome.healing
    cu.spell_slots_expended = outcome.expended
    if outcome.attack is None:
        result.success = True
    hero = str((character or {}).get("name") or "The hero")
    line = narrate(outcome, hero=hero, target=None)
    if outcome.damage:
        line = f"{line} {NO_FOE_NOTE}"
    result.character_update = cu
    result.mechanical_summary = f"{line} {result.mechanical_summary}".strip()


async def _invoke_with_retry(messages: list[dict]) -> AdjudicationResult | None:
    llm = make_chat_model(profile="play.consequence_planner", temperature=0)
    structured_llm = llm.with_structured_output(AdjudicationResult, method="function_calling")
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            result = await traced_call(
                lambda: structured_llm.ainvoke(messages),
                kind="adjudicate",
                profile="play.consequence_planner",
                attempts=attempt,
            )
            if not isinstance(result, AdjudicationResult):
                result = AdjudicationResult.model_validate(result)
            return result
        except Exception as e:
            log.warning(
                "adjudicator_structured_output_failed",
                extra={"attempt": attempt, "error": error_summary(e)},
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

    recall = exact_recall_fact(state, parsed)
    if recall is not None:
        state["exact_recall"] = recall
        citation = recall.get("citation")
        return AdjudicationResult(
            success=True,
            mechanical_summary="You revisit an earned clue.",
            citations=[citation] if citation else [],
            citation_ids=[citation["reference_id"]] if citation else [],
            completion_status="none",
        )

    selected = selected_policy(state, parsed)
    policy_requested = bool(getattr(parsed, "resolution_policy_id", None))
    if policy_requested:
        if (
            in_combat
            or selected is None
            or not selected.get("available")
            or not policy_source_is_fresh(selected, state)
        ):
            state["policy_rejection"] = True
            return AdjudicationResult(
                success=False,
                mechanical_summary="That approach is not available in the current circumstances.",
                completion_status="none",
            )

    # A declared catalog spell is resolved by the same helper the combat round uses (agent.spells).
    spell_outcome: SpellResult | None = None
    request = None
    if not in_combat and selected is None:
        request = cast_request(player_input, parsed, character)
    if request is not None:
        _spell, plan, refusal = request
        if refusal is None and plan is not None and plan.spell.kind == "condition":
            # A condition needs a foe to carry it: outside combat none is tracked, so no slot is spent.
            refusal = (
                f"{_spell.name} is not cast: a condition needs a foe in combat, so no spell effect "
                "is applied."
            )
        if refusal is None and plan is not None and plan.spell.kind == "attack":
            # An attack needs an established target AC: outside combat none is tracked, so no roll and no slot.
            refusal = (
                f"{_spell.name} is not cast: no target is established, so no spell effect is applied. "
                "Name the creature you cast it at, or start an encounter with it."
            )
        if refusal:
            return AdjudicationResult(
                success=False, mechanical_summary=refusal, completion_status="none"
            )
        spell_outcome = resolve_spell(character, plan)
        hero = str((character or {}).get("name") or "The hero")
        extra_outcome = narrate(spell_outcome, hero=hero, target=None)

    check_result = engine_check
    extra_log: list[dict] = []
    if spell_outcome is not None:
        check_result = spell_outcome.attack
        extra_log = list(spell_outcome.dice_log)
    elif selected is not None and not in_combat:
        policy = selected["policy"]
        if policy.mode == "check":
            check_result = resolve_check(character, policy.check, dc=policy.dc, hint=player_input)
            if check_result is None:
                state["policy_rejection"] = True
                return AdjudicationResult(
                    success=False,
                    mechanical_summary="That approach is not available in the current circumstances.",
                    completion_status="none",
                )
            extra_log = list(check_result.dice_log)
        else:
            check_result = None
    elif check_result is None and not in_combat:
        check_result, extra_log = resolve_from_parsed(character, parsed, player_input=player_input)

    # The roll mode is engine-owned; record what was applied before the boundary reads it.
    state[ENGINE_ROLL_MODE_KEY] = check_result.roll_mode if check_result is not None else "normal"
    actions = actions_for_state(state)

    messages = build_adjudicator_prompt(
        state, check_result, extra_outcome=extra_outcome, dice_log=extra_log
    )
    result = (
        await _invoke_with_retry(messages) if selected is None else _neutral(check_result, state)
    )
    if result is None:
        result = _neutral(check_result, state)
    # These fields are engine-owned even if a provider returns undeclared output fields.
    result.accepted_prepared_amendment = None
    if in_combat:
        # Homebrew improvisation never mutates a live combat (EDG-169).
        result.homebrew_proposals = []

    # Declared unsupported mechanics: drop claimed conditions/damage/flags and state the ruling.
    # Runs before the engine's own roll line is stamped on, so engine dice are never stripped.
    applied = engine_applied_for_state(state)
    if spell_outcome is not None:
        applied = replace(applied, executed_spell=True)
    apply_adjudication_boundary(result, actions, applied=applied)

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

    policy_effects: list = []
    if selected is not None:
        # Policy turns accept only reviewed, engine-authored changes. Model proposals are
        # cleared before the normal validators and persistence hooks run.
        policy_effects, reveal_authorizations = policy_effects_for_outcome(
            state, selected, success=result.success
        )
        result.story_effects = policy_effects
        result.character_update = None
        result.damage = None
        result.conditions = []
        result.flags_set = []
        result.flags_cleared = []
        result.scene_id = None
        result.citation_ids = []
        state["authorized_policy_effect_keys"] = list(selected.get("authorized_effect_keys", []))
        state["policy_reveal_authorizations"] = reveal_authorizations

    if spell_outcome is not None and result.character_update is not None:
        # Engine owns spell damage and healing: model-proposed damage and HP numbers are dropped.
        result.character_update.damage_dice = None
        result.character_update.hp_delta = None
    if selected is None:
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
            canonical_inventory=state.get("manifest_status") == "reviewed",
        )
    if spell_outcome is not None:
        _stamp_spell_outcome(result, spell_outcome, character)
    result = enforce_manifest_effects(result, state)
    if not in_combat and selected is None:
        decide_completion(
            result,
            world_flags=state.get("world_flags"),
            parsed=parsed,
            player_input=player_input,
        )
    await prepare_story_validation_state(result, state)
    result = validate_story_effects(result, state)
    if selected is not None:
        accepted = result.accepted_story_effects
        # Persistence validates the canonicalized accepted effects again. Object validation
        # may add a quantity or resolved owner, so authorize exactly that sealed form.
        state["authorized_policy_effect_keys"] = [policy_effect_key(effect) for effect in accepted]
        missing = [
            proposal
            for proposal in policy_effects
            if not any(
                fact.kind == proposal.kind
                and fact.target == proposal.target
                and fact.status == proposal.status
                for fact in accepted
            )
        ]
        if (
            selected.get("generation_failed")
            or len(policy_effects) != selected.get("expected_effect_count", len(policy_effects))
            or len(accepted) != len(policy_effects)
            or missing
        ):
            # A policy is atomic: never commit a discovery while dropping its authored cost,
            # or consume gear without the coupled environmental result.
            result.accepted_story_effects = []
            result.story_effects = []
            result.scene_id = state.get("current_scene_id")
            result.success = check_result.success if check_result else False
            state["story_reveal_context"] = []
            state["authorized_policy_effect_keys"] = []
            state["policy_reveal_authorizations"] = []
            state["policy_rejection"] = True
            result.mechanical_summary = (
                "That approach is not available in the current circumstances."
            )
        if state.get("policy_rejection"):
            state["policy_narration"] = (
                "That approach is not available in the current circumstances."
            )
        else:
            state["policy_narration"] = describe_policy_result(
                result.success, accepted, selected.get("existing_effects", [])
            )
    if selected is None and (state.get("manifest_status") == "reviewed" or result.story_effects):
        safe_story_summary(
            result,
            check_result.describe() if check_result else "The attempt proceeds without a roll.",
        )
    elif selected is not None and not state.get("policy_rejection"):
        result.mechanical_summary = (
            check_result.describe()
            if check_result
            else "The reviewed action proceeds without a roll."
        )
        result.mechanical_summary += " " + state["policy_narration"]
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
    if (
        selected is None
        and not in_combat
        and not result.accepted_story_effects
        and not result.flags_set
        and not result.flags_cleared
        and (not result.scene_id or result.scene_id == state.get("current_scene_id"))
    ):
        receipt = accept_runtime_amendment(
            result.proposed_prepared_amendment, state, in_combat=in_combat
        )
        if receipt is not None:
            result.accepted_prepared_amendment = receipt
            state["accepted_prepared_amendment"] = receipt
    return result


async def rules_adjudicator_node(state: AgentState) -> dict:
    result = await adjudicate(state)
    return {
        "adjudication_result": result,
        "story_reveal_context": state.get("story_reveal_context", []),
        "exact_recall": state.get("exact_recall"),
        "policy_rejection": state.get("policy_rejection", False),
        "policy_narration": state.get("policy_narration"),
        "authorized_policy_effect_keys": state.get("authorized_policy_effect_keys", []),
        "policy_reveal_authorizations": state.get("policy_reveal_authorizations", []),
        "accepted_prepared_amendment": state.get("accepted_prepared_amendment"),
        ENGINE_ROLL_MODE_KEY: engine_applied_for_state(state).roll_mode,
    }
