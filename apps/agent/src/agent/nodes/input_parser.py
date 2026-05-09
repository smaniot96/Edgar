"""Classify the player's message into intent + entities + optional dice expression.

Uses `with_structured_output` so the LLM is forced to return a `ParsedInput` instance rather
than free text we would have to parse. The intent (`combat | rp | exploration`) drives the
graph's conditional edge to the combat subgraph.
"""

from langchain_core.messages import HumanMessage, SystemMessage

from agent.llm import make_chat_model
from agent.models.parsed_input import ParsedInput
from agent.prompts import INPUT_PARSER
from agent.state import AgentState
from edgar_core.config import OPENAI_API_KEY


async def input_parser_node(state: AgentState) -> dict:
    player_input = state.get("player_input")
    if not player_input:
        return {"error": "Missing player_input"}
    if not OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY not set. Add it to Edgar/.env"}

    llm = make_chat_model(temperature=0)
    structured_llm = llm.with_structured_output(ParsedInput, method="function_calling")

    try:
        parsed = await structured_llm.ainvoke(
            [
                SystemMessage(content=INPUT_PARSER),
                HumanMessage(content=player_input),
            ]
        )
    except Exception as e:
        return {"error": str(e)}

    return {"parsed_input": parsed}
