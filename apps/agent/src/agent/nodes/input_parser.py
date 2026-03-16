"""InputParser node: validate and classify player input."""

from agent.state import AgentState
from agent.models.parsed_input import ParsedInput


def input_parser_node(state: AgentState) -> dict:
    """Classify intent and extract entities. TODO: Call LLM with structured output."""
    parsed = ParsedInput(intent="rp", entities={})
    return {"parsed_input": parsed}
