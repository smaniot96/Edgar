"""InputParser node: validate and classify player input."""

import sys
from pathlib import Path

# Ensure project root on path so config loads .env
_root = Path(__file__).resolve().parents[5]  # Edgar/
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from agent.state import AgentState
from agent.models.parsed_input import ParsedInput
from agent.prompts import INPUT_PARSER

# Load .env via config (must run before ChatOpenAI reads env)
import config  # noqa: F401, E402


def input_parser_node(state: AgentState) -> dict:
    """Classify intent and extract entities via LLM."""
    player_input = state.get("player_input")
    if not player_input:
        return {"error": "Missing player_input"}

    if not config.OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY not set. Add it to Edgar/.env"}

    llm = ChatOpenAI(model="gpt-5-mini", temperature=0, api_key=config.OPENAI_API_KEY)
    structured_llm = llm.with_structured_output(ParsedInput, method="function_calling")

    try:
        parsed = structured_llm.invoke([
            SystemMessage(content=INPUT_PARSER),
            HumanMessage(content=player_input),
        ])
    except Exception as e:
        return {"error": str(e)}

    return {"parsed_input": parsed}
