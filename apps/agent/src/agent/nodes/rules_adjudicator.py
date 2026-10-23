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

from agent.history import continuity_context, fit_continuity
from agent.llm import make_chat_model
from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.nodes.manifest_effects import enforce_manifest_effects
from agent.nodes.story_effects import (
    committed_facts,
    is_dm_goal_candidate,
    prepare_story_validation_state,
    safe_story_summary,
    story_candidates,
    validate_story_effects,
)
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
from edgar_core.model_profiles import effective_profile
from edgar_core.sources import citations_for, cited_excerpt, eligible_context
from edgar_core.story import StoryRef, player_story_effect
from edgar_core.token_budget import estimate_tokens

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_MAX_FLAGS_IN_PROMPT = 40


def _planner_evidence_blocks(state, parts, spans):
    """Preserve current event evidence and conversation before duplicate background pages.

    Retrieval remains complete and exact. Only redundant private prompt blocks can be
    omitted, as whole passages: first the global registry, then original source pages,
    then shared chapter prose. Never clip a quotation or discard current event guidance.
    """
    from agent.scene_guidance import observation_excerpt

    blocks = [
        (
            chunk,
            wrap_retrieved_excerpt(
                observation_excerpt(chunk, spans, state.get("rules_edition", "2014"))
            ),
        )
        for chunk in state.get("planner_context") or []
    ]
    budget = effective_profile("play.consequence_planner").max_input_tokens
    if budget is None:
        return [block for _, block in blocks]
    # The newest resolved actions must remain available even when a chapter binds
    # every original PDF page and the global registry into its retrieval context.
    reserve = min(estimate_tokens(continuity_context(state)), budget // 3)
    fixed = estimate_tokens(RULES_ADJUDICATOR + "\n".join(parts)) + 200
    size = sum(estimate_tokens(block) for _, block in blocks)
    removable = sorted(
        range(len(blocks)),
        key=lambda index: (
            0
            if blocks[index][0].get("passage_id") in {"campaign_registry", "campaign_spine"}
            else 1
            if blocks[index][0].get("passage_id", "").startswith("source_page_")
            else 2,
            -index,
        ),
    )
    omitted = set()
    for index in removable:
        if fixed + size + reserve <= budget:
            break
        chunk, block = blocks[index]
        passage_id = chunk.get("passage_id", "")
        if passage_id == "campaign_registry" and any(
            span["reference_id"]
            == citations_for([chunk], state.get("rules_edition", "2014"))[0]["reference_id"]
            for span in spans.values()
        ):
            continue  # Compact actor identity evidence must survive removal of the global dossier.
        if not (
            passage_id in {"campaign_registry", "campaign_spine"}
            or passage_id.startswith("source_page_")
            or (
                passage_id.endswith("_content")
                and chunk.get("scene_ids") != [state.get("current_scene_id")]
            )
        ):
            continue
        omitted.add(index)
        size -= estimate_tokens(block)
    return [block for index, (_, block) in enumerate(blocks) if index not in omitted]


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
    policy_effects: list | None = None,
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
    if policy_effects is not None:
        parts.append(
            "Reviewed selected action: these exact engine-authored story effects occur this turn: "
            + json.dumps([effect.model_dump() for effect in policy_effects])
            + ". Do not repeat, change or replace them. Propose only additional earned "
            "objective/milestone consequences of this action, with a private achievement_basis."
        )
    if parsed is not None and getattr(parsed, "target", None):
        parts.append(f"Target: {parsed.target}")
    from agent.scene_guidance import selected_source_object

    if source_object := selected_source_object(state):
        parts.append(
            "Grounded source-object interaction: "
            + json.dumps(source_object)
            + ". Resolve the source's actual story procedure using the current evidence and "
            "the player's ordinary intent. Do not require a magic phrase or invent a new "
            "prerequisite. Convey its earned story result through source_observations and "
            "any supported object changes through story_effects. This authorizes no spell "
            "damage, conditions, slots or other unsupported mechanical effects."
        )

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
    from agent.history import earned_observation_context

    if earned := earned_observation_context(state):
        parts.append(earned)
    from agent.scene_guidance import observation_excerpt, observation_span_catalog

    answer_spans = (
        observation_span_catalog(state) if state.get("manifest_status") == "reviewed" else {}
    )

    if state.get("resolved_combat_outcomes"):
        parts.append(
            "Authoritative resolved session combat outcomes (cannot be reopened or contradicted): "
            + json.dumps(state["resolved_combat_outcomes"])
        )
    if state.get("manifest_status") == "reviewed":
        # Keep every earned fact and provenance while omitting repeated empty/default
        # fields; long campaigns should spend their prompt budget on actual information.
        facts = [
            fact.model_dump(exclude_none=True, exclude_defaults=True)
            for fact in committed_facts(state)
        ]
        parts.append(
            "Committed session story facts (only these are already earned): " + json.dumps(facts)
        )
        parts.append(
            "Eligible canonical story candidates (definitions, not earned facts): "
            + json.dumps(story_candidates(state))
        )
        from agent.scene_guidance import active_observation_chunks

        observation_refs = [
            {"reference_id": r["reference_id"], "passage_id": c.get("passage_id")}
            for c in active_observation_chunks(state)
            for r in citations_for([c], state.get("rules_edition", "2014"))
        ]
        parts.append(
            "Eligible current-scene evidence references (source_observations select their inline source_span IDs): "
            + json.dumps(observation_refs)
        )
        from agent.scene_guidance import linked_transition_choices

        transitions = linked_transition_choices(state)
        if transitions:
            parts.append(
                "Linked prepared event opportunities with exact proposal templates "
                "(available is not triggered; judge the actual action): " + json.dumps(transitions)
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
            wrap_retrieved_excerpt(
                observation_excerpt(c, answer_spans, state.get("rules_edition", "2014"))
            )
            for c in eligible_context(
                state["adventure_context"], state.get("rules_edition", "2014")
            )
        )
    planner_slot = None
    if state.get("planner_context"):
        parts.append(
            "Private current-scene planner evidence (never disclose secrets in summaries):"
        )
        planner_slot = len(parts)
    if state.get("discovery_entities"):
        parts.append(
            f"Canonical discoveries: {state['discovery_entities']}. Set only their exact discovery_flag to discovered after a justified successful discovery."
        )
    if state.get("manifest_status") == "reviewed":
        parts.append(
            "Resolve the actual action before writing the response: (1) answer each ordinary "
            "question with earned current evidence or a concrete source-supported reason it "
            "cannot be answered; (2) if the action or established resolution now meets one "
            "linked trigger, copy its exact move template into story_effects and add the "
            "actual achievement_basis, stopping at that one scene; (3) propose encounter "
            "effects for current actors directly met or addressed, without revealing their "
            "private knowledge; (4) when the player actually takes an available portable "
            "object, propose its acquire effect, separately from reading/discovering it. "
            "A failed optional inspection does not prevent an independently available "
            "ordinary pickup: propose acquire with its own achievement_basis in a mixed action. "
            "A promise to guide is not travel, but actually following that agreed plan is "
            "an action to resolve. Do not repeatedly describe an obstacle-free journey "
            "starting while omitting its earned linked transition. Preserve prior departures, "
            "custody and answered questions when judging source opening conditions. "
            "An actor's source description fixes their identity and occupation even when the "
            "current scene calls them by first name only. Use eligible registry description "
            "spans for ordinary identity answers; do not disclose private motives or knowledge. "
            "Custody restricts taking or moving a captive, not speaking to a present captive. "
            "Allow conversation unless a recorded physical barrier actually prevents it. "
            "Recognize achieved rescue and safety goals from committed outcomes; do not "
            "repeat their original missing-person or danger premise after it is resolved. "
            "Keep checks scoped to their specific uncertain task; independent player-visible "
            "observations may set requires_check_success=false even after that check fails."
        )
    parts.append(
        "Return citation_ids only for the exact supplied references supporting the ruling. Do not cite unrelated excerpts."
    )

    if planner_slot is not None:
        # Budget against every fixed block, including candidates, obligations and
        # discovery metadata appended after the private-evidence insertion point.
        parts[planner_slot:planner_slot] = _planner_evidence_blocks(state, parts, answer_spans)

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
    from agent.scene_guidance import selected_source_object

    source_object = selected_source_object(state)
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
    if not in_combat and selected is None and source_object is None:
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

    policy_effects: list = []
    reveal_authorizations: list = []
    if selected is not None:
        policy_effects, reveal_authorizations = policy_effects_for_outcome(
            state, selected, success=check_result.success if check_result else True
        )
    dm_goal_targets = set()
    if selected is not None:
        terminal_goals = {
            fact.target.model_dump_json()
            for fact in committed_facts(state)
            if fact.kind in {"objective", "milestone"} and fact.status in {"completed", "failed"}
        }
        dm_goal_targets = {
            target
            for candidate in story_candidates(state)
            if is_dm_goal_candidate(candidate)
            and (target := StoryRef.model_validate(candidate["target"]).model_dump_json())
            not in terminal_goals
        }
    messages = build_adjudicator_prompt(
        state,
        check_result,
        extra_outcome=extra_outcome,
        dice_log=extra_log,
        policy_effects=policy_effects if selected is not None else None,
    )
    result = (
        await _invoke_with_retry(messages)
        if selected is None or dm_goal_targets
        else _neutral(check_result, state)
    )
    if result is None:
        result = _neutral(check_result, state)
    # Retained saved rulings may carry this receipt; live play never creates amendments.
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

    if selected is not None:
        # The same consequence call may recognize a goal, but cannot edit the exact policy
        # contract or propose other mechanics. The normal validator still judges each goal.
        policy_targets = {
            (effect.kind, effect.target.model_dump_json()) for effect in policy_effects
        }
        goal_effects = [
            effect
            for effect in result.story_effects
            if effect.kind in {"objective", "milestone"}
            and effect.target.model_dump_json() in dm_goal_targets
            and (effect.kind, effect.target.model_dump_json()) not in policy_targets
        ]
        result.story_effects = policy_effects + goal_effects
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
            death_mode=str(state.get("death_mode") or "subdued"),
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
        policy_accepted = [
            effect
            for effect in accepted
            if (effect.kind, effect.target.model_dump_json()) in policy_targets
        ]
        # Persistence validates the canonicalized accepted effects again. Object validation
        # may add a quantity or resolved owner, so authorize exactly that sealed form.
        state["authorized_policy_effect_keys"] = [
            policy_effect_key(effect) for effect in policy_accepted
        ]
        missing = [
            proposal
            for proposal in policy_effects
            if not any(
                fact.kind == proposal.kind
                and fact.target == proposal.target
                and fact.status == proposal.status
                for fact in policy_accepted
            )
        ]
        if (
            selected.get("generation_failed")
            or len(policy_effects) != selected.get("expected_effect_count", len(policy_effects))
            or len(policy_accepted) != len(policy_effects)
            or missing
        ):
            # A policy is atomic: never commit a discovery while dropping its authored cost,
            # or consume gear without the coupled environmental result.
            result.accepted_story_effects = []
            result.story_effects = []
            result.scene_id = state.get("current_scene_id")
            result.success = check_result.success if check_result else False
            state["story_reveal_context"] = []
            state["story_arrival_context"] = []
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
                result.success, policy_accepted, selected.get("existing_effects", []),
                result.accepted_observations,
            )
            goal_texts = [
                public["text"]
                for effect in accepted
                if effect.evidence_kind == "dm_adjudication"
                and (public := player_story_effect(effect)) is not None
                and public.get("text")
            ]
            if goal_texts:
                if not policy_accepted and not any(
                    effect.kind == "discover" for effect in selected.get("existing_effects", [])
                ):
                    state["policy_narration"] = (
                        describe_policy_result(result.success, [], [], result.accepted_observations)
                    )
                state["policy_narration"] += "\n\n" + "\n\n".join(dict.fromkeys(goal_texts))
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
    return result


async def rules_adjudicator_node(state: AgentState) -> dict:
    result = await adjudicate(state)
    return {
        "adjudication_result": result,
        "story_reveal_context": state.get("story_reveal_context", []),
        "story_arrival_context": state.get("story_arrival_context", []),
        "exact_recall": state.get("exact_recall"),
        "policy_rejection": state.get("policy_rejection", False),
        "policy_narration": state.get("policy_narration"),
        "authorized_policy_effect_keys": state.get("authorized_policy_effect_keys", []),
        "policy_reveal_authorizations": state.get("policy_reveal_authorizations", []),
        ENGINE_ROLL_MODE_KEY: engine_applied_for_state(state).roll_mode,
    }
