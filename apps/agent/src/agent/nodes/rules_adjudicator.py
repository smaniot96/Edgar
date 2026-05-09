"""RulesAdjudicator node: apply mechanics, call dice tool, produce structured outcome."""

from agent.llm import make_chat_model
from agent.state import AgentState
from agent.models.adjudication import AdjudicationResult
from agent.prompts import RULES_ADJUDICATOR
from tools import roll


async def rules_adjudicator_node(state: AgentState) -> dict:
    """Apply rules and dice. LLM proposes; dice tool decides numbers."""
    player_input = state.get("player_input", "")
    parsed_input = state.get("parsed_input")
    rules_context = state.get("rules_context", [])
    if not rules_context:
        rules_context = [
            c
            for c in state.get("retrieved_context", [])
            if c.get("kind") == "rules"
        ]

    dice_result = None
    dice_expression = None
    if parsed_input and parsed_input.dice_expression:
        dice_expression = parsed_input.dice_expression
        try:
            outcome = roll(dice_expression)
            dice_result = outcome.total
        except ValueError as e:
            return {"error": f"Invalid dice: {e}"}

    # Build context for LLM
    context_parts = [f"Player: {player_input}"]
    if rules_context:
        context_parts.append("Official rules excerpts (Player Handbook, DMG, Monster Manual):")
        for c in rules_context:
            context_parts.append(f"- {c.get('text', '')} (source: {c.get('source', '')})")
    if dice_result is not None:
        context_parts.append(f"Dice roll ({dice_expression}): {dice_result}")

    character = state.get("character")
    if character:
        inv = character.get("inventory")
        context_parts.append(
            f"Player character: {character['name']}, level {character['level']} {character['class']}, "
            f"HP {character['hp_current']}/{character['hp_max']}, stats {character['stats']}, "
            f"inventory {inv}"
        )

    llm = make_chat_model(temperature=0)
    structured_llm = llm.with_structured_output(AdjudicationResult, method="function_calling")

    try:
        result = await structured_llm.ainvoke([
            {"role": "system", "content": RULES_ADJUDICATOR},
            {"role": "user", "content": "\n".join(context_parts)},
        ])
        if dice_result is not None:
            result.dice_result = dice_result
        return {"adjudication_result": result}
    except Exception as e:
        return {"error": str(e)}
