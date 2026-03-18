"""RulesAdjudicator node: apply mechanics, call dice tool, produce structured outcome."""

import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[5]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config  # noqa: F401, E402

from langchain_openai import ChatOpenAI

from agent.state import AgentState
from agent.models.adjudication import AdjudicationResult
from agent.prompts import RULES_ADJUDICATOR
from tools import roll


def rules_adjudicator_node(state: AgentState) -> dict:
    """Apply rules and dice. LLM proposes; dice tool decides numbers."""
    player_input = state.get("player_input", "")
    parsed_input = state.get("parsed_input")
    retrieved_context = state.get("retrieved_context", [])

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
    if retrieved_context:
        context_parts.append("Relevant rules/lore:")
        for c in retrieved_context:
            context_parts.append(f"- {c.get('text', '')} (source: {c.get('source', '')})")
    if dice_result is not None:
        context_parts.append(f"Dice roll ({dice_expression}): {dice_result}")

    llm = ChatOpenAI(model="gpt-5-mini", temperature=0, api_key=config.OPENAI_API_KEY)
    structured_llm = llm.with_structured_output(AdjudicationResult, method="function_calling")

    try:
        result = structured_llm.invoke([
            {"role": "system", "content": RULES_ADJUDICATOR},
            {"role": "user", "content": "\n".join(context_parts)},
        ])
        if dice_result is not None:
            result.dice_result = dice_result
        return {"adjudication_result": result}
    except Exception as e:
        return {"error": str(e)}
