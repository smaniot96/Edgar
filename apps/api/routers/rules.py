"""Read-only description of the simplified rules the engine executes (EDG-028).

Static, code-owned data: no database or model access. Clients use it to explain which mechanics
are simulated and which declared actions receive narrative rulings instead of engine effects.
"""

from fastapi import APIRouter

from agent.supported_rules import contract_payload

router = APIRouter(tags=["rules"])


@router.get("/rules/supported-scope")
def supported_scope() -> dict:
    return contract_payload()
