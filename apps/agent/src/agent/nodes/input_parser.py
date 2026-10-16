"""Classify the player's message into intent + a *proposed* check (never numbers).

Uses `with_structured_output` so the LLM is forced to return a `ParsedInput` instance rather
than free text we would have to parse. The intent (`combat | rp | exploration`) drives the
graph's conditional edge to the combat subgraph. The proposed `check`/`difficulty` are turned
into a modifier, DC and deterministic success by `agent.resolution`.

The player's text is wrapped in <player_action> tags and treated as untrusted. If structured
output fails (provider hiccup, malformed tool call) the turn still proceeds as plain
exploration with no check, rather than failing.
"""

import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from agent.action_economy import has_attack_words
from agent.combatants import is_standing
from agent.history import fit_continuity, public_earned_facts
from agent.llm import make_chat_model
from agent.models.parsed_input import ParsedInput
from agent.prompts import INPUT_PARSER, wrap_player_action
from agent.resolution import PENDING_COMPLETION_FLAG, normalise_check
from agent.resolution_policies import parser_earned_clues, parser_policy_choices
from agent.state import AgentState
from edgar_core.config import OPENAI_API_KEY

log = logging.getLogger(__name__)

# Avoiding, sneaking past, refusing or talking about a fight (EDG-126). Such a line starts no combat
# unless it also declares a hostile action.
_NO_FIGHT = re.compile(
    r"\b(?:avoid\w*|evad\w*|sneak\w*|sidestep\w*|slip\s+past|negotiat\w*|talk\w*|discuss\w*|parley\w*"
    r"|(?:won't|will\s+not|don't|do\s+not|rather\s+not|refuse\s+to|without|instead\s+of)\s+(?:\w+\s+){0,2}"
    r"(?:fight\w*|combat|battle))\b",
    re.IGNORECASE,
)


def _parser_context(state: AgentState) -> str:
    """Minimal scene context so `retrieval_query`/`target` can be grounded."""
    parts: list[str] = []
    scene = state.get("current_scene_id")
    if scene:
        parts.append(f"Current scene id: {scene}")
    npcs = [
        n.get("name") for n in (state.get("npcs") or []) if isinstance(n, dict) and n.get("name")
    ]
    if npcs:
        parts.append(f"Known NPCs: {', '.join(npcs[:12])}")
    combat = state.get("combat_state") or {}
    if combat and not combat.get("ended"):
        foes = [
            c.get("name")
            for c in combat.get("combatants") or []
            if not c.get("is_player") and is_standing(c)
        ]
        if foes:
            parts.append(f"Combat in progress against: {', '.join(foes)}")
    if (state.get("world_flags") or {}).get(PENDING_COMPLETION_FLAG):
        parts.append("The DM just asked the player whether they want to end the adventure here.")
    policies = parser_policy_choices(state)
    if policies:
        parts.append(
            "Reviewed current-scene action choices (select a policy key only when it directly matches the player's approach; unavailable choices must not be selected): "
            + str(policies)
        )
    clues = parser_earned_clues(state)
    if clues:
        parts.append(
            "Session-earned public clues, available only for an explicit reread request; choose the exact fact_id when the player names or refers to that clue: "
            + str(clues)
        )
    continuity_slot = len(parts)
    facts = public_earned_facts(state)
    if facts:
        parts.append(f"Committed player-known story facts and exact source quotations: {json.dumps(facts)}")
    outcomes = [
        {"id": outcome.get("id"), "outcome": outcome.get("outcome"), "round": outcome.get("round")}
        for outcome in state.get("resolved_combat_outcomes") or []
    ]
    if outcomes:
        parts.append(
            "Authoritative resolved combat outcomes (never reopen or contradict): "
            + json.dumps(outcomes)
        )
    if continuity := fit_continuity(
        state,
        "play.parser",
        [INPUT_PARSER, wrap_player_action(state.get("player_input")), *parts],
    ):
        parts.insert(continuity_slot, continuity)
    return "\n".join(parts)


async def input_parser_node(state: AgentState) -> dict:
    player_input = state.get("player_input")
    if not player_input:
        return {"error": "Missing player_input"}
    if not OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY not set. Add it to Edgar/.env"}

    llm = make_chat_model(profile="play.parser", temperature=0)
    structured_llm = llm.with_structured_output(ParsedInput, method="function_calling")

    context = _parser_context(state)
    user_content = wrap_player_action(player_input)
    if context:
        user_content = f"{context}\n\n{user_content}"

    try:
        parsed = await structured_llm.ainvoke(
            [
                SystemMessage(content=INPUT_PARSER),
                HumanMessage(content=user_content),
            ]
        )
        if not isinstance(parsed, ParsedInput):
            parsed = ParsedInput.model_validate(parsed)
    except Exception as e:
        # Degrade, don't fail: no check is rolled and retrieval uses the raw input.
        log.warning("input_parser_fallback", extra={"error": str(e)[:200]})
        parsed = ParsedInput(intent="exploration")

    norm = normalise_check(parsed.check)
    attacks = norm is not None and norm[1] in ("attack", "spell_attack")
    if parsed.intent == "combat" and _NO_FIGHT.search(player_input):
        if not attacks and not has_attack_words(player_input):
            # Avoiding or talking about a fight declares no hostile action, so it starts no combat.
            parsed = parsed.model_copy(update={"intent": "exploration"})

    return {"parsed_input": parsed}
