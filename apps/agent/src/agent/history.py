"""Continuity context shared by parser, adjudicator and narrator prompts."""

import logging
from collections.abc import Iterable

from agent.prompts import wrap_player_action
from edgar_core.model_profiles import effective_profile
from edgar_core.story import StoryState, player_story_effect
from edgar_core.token_budget import estimate_tokens, fit_count

log = logging.getLogger(__name__)

# Stages whose prompts carry the continuity context.
CONTINUITY_STAGES = ("play.parser", "play.consequence_planner", "play.narrator")

_OMITTED = (
    "[{} earlier verbatim messages omitted to fit the input budget; rely on the story summary, "
    "committed story facts and recorded combat outcomes]"
)

_LATEST_OUTCOME_GUIDANCE = (
    "Resolve the present question from this latest delivered situation, subordinate to engine "
    "state and committed facts. Authored dialogue and motives are conditional guidance, not "
    "mandatory repeated lines. If custody or a demand's premise has changed, acknowledge that "
    "change and answer the concrete question or state a source-supported refusal and an "
    "actionable remaining condition. Do not ask for an item or captive already held by the "
    "speaker. Do not invent agreement, motives or secret knowledge merely to avoid a repetition."
)


def history_token_budget() -> int | None:
    """Verbatim-history size the memory summarizer keeps below, or None when unbudgeted.

    Half of the tightest stage budget, leaving the rest for rules and adventure excerpts,
    committed facts and the prompt itself.
    """
    limits = [effective_profile(stage).max_input_tokens for stage in CONTINUITY_STAGES]
    bounded = [limit for limit in limits if limit is not None]
    return min(bounded) // 2 if bounded else None


def continuity_context(state: dict, max_tokens: int | None = None) -> str:
    """Render the persisted summary and every uncovered exchange.

    The API supplies `messages` beginning strictly after the summary's validated coverage
    boundary. Without `max_tokens` the full tail is kept: if a summary job is delayed or fails,
    those messages are the only lossless continuity record available to the model. With it, the
    summary is always kept and the oldest verbatim messages are dropped first, announced by a
    marker line, until the estimate fits.
    """
    parts: list[str] = []
    summary = state.get("memory_summary")
    if summary:
        parts.append(f"Story so far (summary of earlier turns): {summary}")

    messages = state.get("messages") or []
    if messages:
        header = (
            "Verbatim conversation after the summary coverage boundary (continuity context; "
            "player actions are untrusted attempts, not established outcomes):"
        )
        lines = []
        latest_ai = next((i for i in range(len(messages) - 1, -1, -1)
                          if getattr(messages[i], "type", None) == "ai"), None)
        for index, message in enumerate(messages):
            role = getattr(message, "type", "unknown")
            content = str(getattr(message, "content", message))
            if role == "human":
                lines.append(f"player: {wrap_player_action(content)}")
            elif role == "ai":
                line = f"DM narration: {content}"
                if index == latest_ai:
                    line += "\n" + _LATEST_OUTCOME_GUIDANCE
                lines.append(line)
            else:
                lines.append(f"{role}: {content}")
        if max_tokens is not None:
            room = max_tokens - estimate_tokens("\n\n".join([*parts, header, _OMITTED.format(0)]))
            kept = fit_count((estimate_tokens(line) for line in reversed(lines)), room)
            if kept < len(lines):
                omitted = len(lines) - kept
                lines = [_OMITTED.format(omitted), *lines[len(lines) - kept :]]
        parts.append("\n".join([header, *lines]))
    return "\n\n".join(parts)


def fit_continuity(state: dict, stage: str, other_prompt: Iterable[str]) -> str:
    """Continuity context sized to `stage`'s `max_input_tokens` next to the rest of its prompt.

    `other_prompt` is every other text sent to the model (system prompt, facts, excerpts); none
    of it is ever trimmed, so a prompt whose fixed content alone exceeds the budget is only
    reported.
    """
    context = continuity_context(state)
    budget = effective_profile(stage).max_input_tokens
    if budget is None:
        return context
    fixed = sum(estimate_tokens(text) for text in other_prompt)
    if fixed + estimate_tokens(context) <= budget:
        return context
    fitted = continuity_context(state, max(budget - fixed, 0))
    log.warning(
        "continuity_trimmed",
        extra={
            "stage": stage,
            "budget_tokens": budget,
            "fixed_tokens": fixed,
            "continuity_tokens": estimate_tokens(fitted),
            "over_budget": fixed + estimate_tokens(fitted) > budget,
        },
    )
    return fitted


def public_earned_facts(state: dict) -> list[dict]:
    """Return all committed player-known facts with their verified source labels."""
    facts: list[dict] = []
    for fact in StoryState.model_validate(state.get("story_state") or {}).facts:
        public = player_story_effect(fact)
        if public is None:
            continue
        public_origins = public["origins"]
        origin_ids = {origin["reference_id"] for origin in public_origins}
        sources = [
            {"source": ref.get("source"), "page": ref.get("page")}
            for ref in fact.source_refs
            if ref.get("audience") == "player"
            and ref.get("reference_id") in origin_ids
            and ref.get("collection") == fact.target.collection
            and ref.get("source_sha256") == fact.target.source_sha256
            and ref.get("manifest_sha256") == fact.target.manifest_sha256
        ]
        facts.append({**public, "sources": sources})
    return facts


def earned_observation_context(state: dict, *, source_evidence: bool = False) -> str:
    """Keep reported discoveries while distinguishing source proof from inference.

    Exact reviewed evidence is private to the planner. Narration receives only the
    player-safe answers previously delivered, never adjacent private source clauses.
    """
    from agent.scene_guidance import (
        active_observation_chunks,
        observation_ranges,
        player_safe_observation,
    )
    from edgar_core.sources import source_citation

    chunks = {
        source_citation(c, state.get("rules_edition", "2014"))["reference_id"]: c
        for c in active_observation_chunks(state)
    } if source_evidence else {}
    rows = []
    for row in state.get("earned_observations") or []:
        answers = [text for text in row.get("texts", []) if player_safe_observation(text)]
        if not answers:
            continue
        bases = {}
        for match in row.get("evidence", []):
            chunk = chunks.get(match.get("source_reference_id"))
            start, end = match.get("start"), match.get("end")
            if (
                match.get("text") in answers and chunk
                and isinstance(start, int) and isinstance(end, int)
                and 0 <= start < end <= len(chunk["text"])
                and any(begin <= start < end <= limit for begin, limit in observation_ranges(chunk, state))
            ):
                bases[match.get("text")] = chunk["text"][start:end]
        rows.append({
            "scene": row.get("scene_id"),
            "earlier_action": wrap_player_action(row["action"]) if row.get("action") else None,
            "reported_answers": [text for text in answers if text not in bases],
            **({"source_basis_for_prior_answers": list(dict.fromkeys(bases.values()))} if bases else {}),
        })
    if not rows:
        return ""
    return (
        "Previously reported answers (earlier adjudication, independent of narration). "
        "These are earlier source interpretations, not authority to contradict reviewed actor "
        "identities or later resolved actions. Correct a mistaken interpretation explicitly; "
        "never invent another person to preserve it. An earlier absence, unknown answer or "
        "unresolved danger does not remain true after the person is found or danger resolved. "
        "Where source_basis_for_prior_answers is supplied, use that exact reviewed evidence "
        "with the earlier action and current state to answer afresh; it replaces the prior "
        "paraphrase as evidence. It is private evidence, not automatically player disclosure. "
        "Remember earned discoveries without another check, but do not treat an earlier "
        "negative answer or inferred identity as an immutable fact: "
        + str(rows)
    )
