"""Apply rules to the player's action and produce a structured outcome.

The LLM proposes; the dice tool decides numbers. If `parsed_input.dice_expression` is set
(e.g. "1d20+3"), `tools.dice.roll` is called *before* the LLM and the total is injected into
the prompt; the LLM is also told the dice result, so its `mechanical_summary` is consistent.
We then overwrite `result.dice_result` to be sure the LLM did not hallucinate a different number.

Only the *rules* bucket goes into the prompt (PHB/DMG/MM). Adventure-module text is
narrator-only so the adjudicator cannot rule on flavour.
"""

from agent.llm import make_chat_model
from agent.models.adjudication import AdjudicationResult
from agent.prompts import RULES_ADJUDICATOR
from agent.state import AgentState
from tools import roll


async def rules_adjudicator_node(state: AgentState) -> dict:
    player_input = state.get("player_input", "")
    parsed_input = state.get("parsed_input")

    # Prefer the split bucket; fall back to the merged legacy view filtered by `kind`.
    rules_context = state.get("rules_context", [])
    if not rules_context:
        rules_context = [c for c in state.get("retrieved_context", []) if c.get("kind") == "rules"]

    dice_result: int | None = None
    dice_expression: str | None = None
    if parsed_input and parsed_input.dice_expression:
        dice_expression = parsed_input.dice_expression
        try:
            dice_result = roll(dice_expression).total
        except ValueError as e:
            return {"error": f"Invalid dice: {e}"}

    context_parts = [f"Player: {player_input}"]
    if rules_context:
        context_parts.append("Official rules excerpts (Player Handbook, DMG, Monster Manual):")
        for c in rules_context:
            context_parts.append(f"- {c.get('text', '')} (source: {c.get('source', '')})")
    if dice_result is not None:
        context_parts.append(f"Dice roll ({dice_expression}): {dice_result}")

    character = state.get("character")
    if character:
        context_parts.append(
            f"Player character: {character['name']}, level {character['level']} {character['class']}, "
            f"HP {character['hp_current']}/{character['hp_max']}, stats {character['stats']}, "
            f"inventory {character.get('inventory')}"
        )

    llm = make_chat_model(temperature=0)
    structured_llm = llm.with_structured_output(AdjudicationResult, method="function_calling")

    try:
        result = await structured_llm.ainvoke(
            [
                {"role": "system", "content": RULES_ADJUDICATOR},
                {"role": "user", "content": "\n".join(context_parts)},
            ]
        )
    except Exception as e:
        return {"error": str(e)}

    # Authoritative dice value: the LLM is told what we rolled, but we overwrite anyway so a
    # bad model output never alters game numbers.
    if dice_result is not None:
        result.dice_result = dice_result
    return {"adjudication_result": result}
