from app.agents.local_agent import evaluate_accommodations
from app.core.state import GraphState


async def local_agent_node(state: GraphState) -> dict:
    """GraphState → local_agent 호출 → 부분 state 반환."""
    user_input = state["user_input"]
    accommodations = state["candidate_accommodations"]
    stay_dates = state.get("parsed_preferences", {}).get("stay_dates")

    evaluations = await evaluate_accommodations(
        accommodations=accommodations,
        user_input=user_input,
        stay_dates=stay_dates,
    )

    retry_count = state.get("retry_count", {}).copy()
    retry_count["local"] = retry_count.get("local", 0)

    return {
        "local_evaluations": evaluations,
        "retry_count": retry_count,
    }
