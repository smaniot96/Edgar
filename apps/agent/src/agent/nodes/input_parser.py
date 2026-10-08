"""Classify the player's message into intent + a *proposed* check (never numbers).

Uses `with_structured_output` so the LLM is forced to return a `ParsedInput` instance rather
than free text we would have to parse. The intent (`combat | rp | exploration`) drives the
graph's conditional edge to the combat subgraph. The proposed `check`/`difficulty` are turned
into a modifier, DC and deterministic success by `agent.resolution`.

The player's text is wrapped in <player_action> tags and treated as untrusted. If structured
output fails (provider hiccup, malformed tool call) the turn still proceeds as plain
exploration with no check, rather than failing.
"""

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from agent.llm import make_chat_model
from agent.models.parsed_input import ParsedInput
from agent.prompts import INPUT_PARSER, wrap_player_action
from agent.resolution import PENDING_COMPLETION_FLAG
from agent.state import AgentState
from edgar_core.config import OPENAI_API_KEY

log = logging.getLogger(__name__)


def _parser_context(state: AgentState) -> str:
    """Minimal scene context so `retrieval_query`/`target` can be grounded."""
    parts: list[str] = []
    scene = state.get("current_scene_id")
    if scene:
        parts.append(f"Current scene id: {scene}")
    npcs = [n.get("name") for n in (state.get("npcs") or []) if isinstance(n, dict) and n.get("name")]
    if npcs:
        parts.append(f"Known NPCs: {', '.join(npcs[:12])}")
    combat = state.get("combat_state") or {}
    if combat and not combat.get("ended"):
        foes = [
            c.get("name")
            for c in combat.get("combatants") or []
            if not c.get("is_player") and c.get("alive", True)
        ]
        if foes:
            parts.append(f"Combat in progress against: {', '.join(foes)}")
    if (state.get("world_flags") or {}).get(PENDING_COMPLETION_FLAG):
        parts.append(
            "The DM just asked the player whether they want to end the adventure here."
        )
    return "\n".join(parts)


async def input_parser_node(state: AgentState) -> dict:
    player_input = state.get("player_input")
    if not player_input:
        return {"error": "Missing player_input"}
    if not OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY not set. Add it to Edgar/.env"}

    llm = make_chat_model(temperature=0)
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

    return {"parsed_input": parsed}
