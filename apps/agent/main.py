"""Entry point for the agent. Run from Edgar: uv run --project apps/agent python -m main"""

import asyncio

from agent.graph import app


def main():
    """Demo: invoke graph with minimal state."""
    state = {"player_input": "I look around the tavern.", "session_id": 1}
    result = asyncio.run(app.ainvoke(state))
    if result.get("error"):
        print("Error:", result["error"])
    else:
        print("Narration:", result.get("narration", "(none)"))
        if result.get("combat_state"):
            print("Combat:", result["combat_state"])


if __name__ == "__main__":
    main()
