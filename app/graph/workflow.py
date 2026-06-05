"""
목표 그래프 구조 (docs/orchestration_worker.md 참고):

    planner_start
       └─ 줄글 파싱 · UserInput 구조화 · 5개 조건 해석 · priority_weights 산출
            ↓
    human_select  ← Stay Phase 1 결과로 사용자 지역 선택 (Human-in-the-loop)
            ↓
    planner_finish
       └─ Stay Phase 2 (숙소 탐색)
          → [동적 워커 선택] Living / Work(조건부) / Local(조건부) 병렬 실행
          → 코드 기반 가중 평균 종합 점수 계산
          → LLM 정성 요약 생성
          → 최종 출력
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.core.state import GraphState
from app.graph.nodes import human_select_node, planner_finish_node, planner_start_node

_builder = StateGraph(GraphState)

_builder.add_node("planner_start",  planner_start_node)
_builder.add_node("human_select",   human_select_node)
_builder.add_node("planner_finish", planner_finish_node)

_builder.set_entry_point("planner_start")
_builder.add_edge("planner_start",  "human_select")
_builder.add_edge("human_select",   "planner_finish")
_builder.add_edge("planner_finish", END)

memory = MemorySaver()
graph = _builder.compile(checkpointer=memory)
