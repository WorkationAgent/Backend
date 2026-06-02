from langgraph.types import interrupt

from app.agents.planner_agent import planner_phase1, planner_phase2
from app.core.state import GraphState


async def planner_start_node(state: GraphState) -> dict:
    return await planner_phase1(state)


async def human_select_node(state: GraphState) -> dict:
    """지역 후보를 호출자에게 반환하고 선택을 기다린다.

    재개 방법:
        from langgraph.types import Command
        graph.invoke(Command(resume=selected_region_dict), config=config)
    """
    selected = interrupt({"candidate_regions": state.get("candidate_regions", [])})
    return {"selected_region": selected}


async def planner_finish_node(state: GraphState) -> dict:
    return await planner_phase2(state)
