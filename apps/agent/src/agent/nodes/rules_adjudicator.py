"""RulesAdjudicator node: apply mechanics, call dice tool, produce structured outcome."""

from agent.state import AgentState
from agent.models.adjudication import AdjudicationResult


def rules_adjudicator_node(state: AgentState) -> dict:
    """Apply rules and dice. TODO: Call LLM + dice tool."""
    result = AdjudicationResult(success=True, mechanical_summary="")
    return {"adjudication_result": result}
