"""
LangGraph 노드 정의 — 멀티에이전트 워케이션 추천 그래프.

각 노드는 (state) → 부분 state dict(델타) 를 반환한다.
흐름 제어/배선은 app/graph/workflow.py 가 담당한다.

흐름:
    parse → interpret → region_search → human_select (interrupt)
          → accommodation_search → normalize
          → [동적 fan-out] work? / living? / local?  (병렬)
          → integrate (fan-in) → END

    숙소를 못 찾으면 normalize 이후 곧장 END (LLM 호출 없이, 환각 방지).

주의: 병렬 워커가 동시에 쓰는 errors/warnings/retry_count 채널은
      GraphState의 리듀서가 병합한다. 따라서 모든 노드는 '델타'만 반환해야 하며
      {**state, ...} 형태로 전체 state를 되돌려주면 안 된다(리듀서 중복 적용).
"""

from __future__ import annotations

from langgraph.types import interrupt

from app.agents.living_agent import living_agent
from app.agents.local_agent import local_agent
from app.agents.stay_agent import accommodation_search_node, region_search_node
from app.agents.work_agent import work_agent
from app.agents.planner_agent import (
    build_final_output,
    build_skipped_agents,
    interpret_user_input,
    normalize_accommodations,
    parse_raw_input,
)
from app.core.state import GraphState

# stay_agent의 두 노드는 이미 (state) → 델타 시그니처라 그대로 그래프에 등록한다.
__all__ = [
    "parse_node",
    "interpret_node",
    "region_search_node",
    "human_select_node",
    "accommodation_search_node",
    "normalize_node",
    "work_node",
    "living_node",
    "local_node",
    "integrate_node",
]


# ── Planner: 입력 파싱 & 조건 해석 ────────────────────────────────────────────

async def parse_node(state: GraphState) -> dict:
    """줄글 입력 → UserInput 구조화 + 제외 지역."""
    raw_text: str = state.get("raw_user_input", "")
    user_input, excluded_regions = await parse_raw_input(raw_text)
    return {"user_input": user_input, "excluded_regions": excluded_regions}


async def interpret_node(state: GraphState) -> dict:
    """UserInput → 조건 해석(must/avoid/preference) + priority_weights 산출."""
    return await interpret_user_input(state["user_input"])


# ── Human-in-the-loop ─────────────────────────────────────────────────────────

def human_select_node(state: GraphState) -> dict:
    """지역 후보를 호출자에게 반환하고 사용자의 선택을 기다린다(interrupt).

    재개:
        graph.ainvoke(Command(resume=<선택 지역 dict>), config=config)
    """
    selected = interrupt({"candidate_regions": state.get("candidate_regions", [])})
    return {"selected_region": selected}


# ── 정규화 + 디스패치 준비 ────────────────────────────────────────────────────

def normalize_node(state: GraphState) -> dict:
    """Stay 숙소 출력 → 워커 공통 포맷 정규화 + 스킵 워커 사유 계산.

    숙소가 없으면 errors만 남기고 워커를 건너뛴다(LLM 호출 없이 환각 방지).
    이후 라우팅(route_after_normalize)이 빈 결과를 보고 END로 보낸다.
    """
    raw = state.get("candidate_accommodations") or []
    if not raw:
        return {
            "candidate_accommodations": [],
            "errors": ["해당 지역에서 실제 숙소를 찾지 못했습니다."],
        }
    normalized = normalize_accommodations(raw)
    skipped = build_skipped_agents(state.get("priority_weights") or {})
    return {"candidate_accommodations": normalized, "skipped_agents": skipped}


# ── 병렬 워커 노드 ────────────────────────────────────────────────────────────
# 각 워커는 자신의 평가 키(+warnings/retry_count)만 델타로 반환한다.
# 한 워커가 예외로 죽어도 나머지 워커와 통합은 진행되도록 노드 단위로 격리한다
# (기존 asyncio.gather(return_exceptions=True) 동작 보존). 빈 평가 + errors 델타 반환.

async def work_node(state: GraphState) -> dict:
    try:
        return await work_agent(state)
    except Exception as e:
        return {"work_evaluations": [], "errors": [f"work agent 실패 — {e}"]}


async def living_node(state: GraphState) -> dict:
    try:
        return await living_agent(state)
    except Exception as e:
        return {"living_evaluations": [], "errors": [f"living agent 실패 — {e}"]}


async def local_node(state: GraphState) -> dict:
    try:
        return await local_agent(state)
    except Exception as e:
        return {"local_evaluations": [], "errors": [f"local agent 실패 — {e}"]}


# ── 통합: 최종 추천 순위 ──────────────────────────────────────────────────────

async def integrate_node(state: GraphState) -> dict:
    """워커 평가를 종합해 최종 추천 순위를 생성한다(점수=코드, 요약=LLM)."""
    try:
        final = await build_final_output(
            normalized=state.get("candidate_accommodations") or [],
            work_evals=state.get("work_evaluations") or [],
            living_evals=state.get("living_evaluations") or [],
            local_evals=state.get("local_evaluations") or [],
            state=state,
        )
        return {"final_user_output": final}
    except Exception as e:
        return {"errors": [f"planner: build_final_output 실패 — {e}"]}
