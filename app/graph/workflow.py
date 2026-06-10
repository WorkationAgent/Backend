"""
워케이션 추천 멀티에이전트 그래프 (LangGraph).

    START → parse → interpret → region_search → human_select (interrupt)
          → accommodation_search → normalize
          → [동적 fan-out] work / living / local  (priority_weights 기준 부분 병렬)
          → integrate (fan-in) → END

    숙소를 못 찾으면 normalize에서 곧장 END (LLM 호출 없이, 환각 방지).

HITL: human_select 노드의 interrupt()로 중단, Command(resume=...)로 재개.
      MemorySaver 체크포인터 + config의 thread_id로 세션을 식별한다.
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.planner_agent import select_workers
from app.core.state import GraphState
from app.graph.nodes import (
    accommodation_search_node,
    human_select_node,
    integrate_node,
    interpret_node,
    living_node,
    local_node,
    normalize_node,
    parse_node,
    region_search_node,
    work_node,
)


def route_after_normalize(state: GraphState):
    """정규화 후 동적 라우팅.

    - 숙소 없음        → END (워커 전부 건너뜀)
    - 실행할 워커 있음 → 해당 워커 노드들로 fan-out (병렬)
    - 모두 스킵        → integrate로 직행 (stay_score만으로 최종 산출)
    """
    if not state.get("candidate_accommodations"):
        return END
    workers = select_workers(state.get("priority_weights") or {})
    return workers or "integrate"


_builder = StateGraph(GraphState)

_builder.add_node("parse",                parse_node)
_builder.add_node("interpret",            interpret_node)
_builder.add_node("region_search",        region_search_node)
_builder.add_node("human_select",         human_select_node)
_builder.add_node("accommodation_search", accommodation_search_node)
_builder.add_node("normalize",            normalize_node)
_builder.add_node("work",                 work_node)
_builder.add_node("living",               living_node)
_builder.add_node("local",                local_node)
_builder.add_node("integrate",            integrate_node)

# ── 직선 구간: 입력 해석 → 지역 탐색 → (HITL) → 숙소 탐색 → 정규화 ──────────────
_builder.add_edge(START, "parse")
_builder.add_edge("parse", "interpret")
_builder.add_edge("interpret", "region_search")
_builder.add_edge("region_search", "human_select")
_builder.add_edge("human_select", "accommodation_search")
_builder.add_edge("accommodation_search", "normalize")

# ── 동적 fan-out: priority_weights에 따라 실행할 워커만 병렬 기동 ──────────────
_builder.add_conditional_edges(
    "normalize",
    route_after_normalize,
    ["work", "living", "local", "integrate", END],
)

# ── fan-in: 실행된 워커는 모두 integrate로 모인다 ─────────────────────────────
_builder.add_edge("work", "integrate")
_builder.add_edge("living", "integrate")
_builder.add_edge("local", "integrate")
_builder.add_edge("integrate", END)

# HITL을 위해 체크포인터 필수 (현재: 인메모리)
memory = MemorySaver()
graph = _builder.compile(checkpointer=memory)
