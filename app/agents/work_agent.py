"""
Work Agent (업무 환경 평가 에이전트)

State 입출력:
    읽기: state["candidate_accommodations"]  (숙소 후보)
          state["user_input"]               (사용자 원본 입력)
          state["selected_region"]          (선택된 생활권, region_name 추출용)
    쓰기: state["work_evaluations"]          (숙소별 평가 결과)

흐름:
    1. LLM으로 사용자 조건에서 필수/선호 조건 추출 (숙소 전체 공통)
    2. 숙소마다:
       a. place_tool로 주변 작업 공간 검색 (이동수단 기반 반경 분기)
       b. 결과 없으면 반경 확장 재시도
       c. search_tool로 상위 3곳 네이버 후기 수집 및 추정값 보정
       d. LLM으로 업무 환경 종합 평가
"""

from __future__ import annotations

import json

from app.core.llm import call_llm
from app.config.settings import (
    SEARCH_RADIUS_CAR_KM,
    SEARCH_RADIUS_WALK_KM,
    RETRY_CONFIDENCE_THRESHOLD,
    RETRY_MAX_COUNT,
    RETRY_RADIUS_EXPAND_KM,
)
from app.core.state import GraphState
from app.prompts.work_prompts import (
    WORK_EVALUATE_SYSTEM,
    WORK_EVALUATE_USER,
    WORK_REQUIREMENTS_SYSTEM,
    WORK_REQUIREMENTS_USER,
)
from app.schemas.user_input import UserInput
from app.schemas.worker import WorkEvaluation
from app.tools.place_tool import is_car_transport, search_workplaces
from app.tools.search_tool import search_workplace_reviews


#---- _enrich_with_reviews() — 후기 데이터로 정보 보정----
def _enrich_with_reviews(
    workplaces: list[dict],
    region_name: str = "",
    top_n: int = 3,
) -> list[dict]:
    """가장 가까운 top_n개 작업 공간에 네이버 후기를 추가하고 추정값을 보정한다.

    search_tool에서 추출한 wifi/outlet/long_stay 값이 있으면
    place_tool의 장소 유형 기반 추정값을 실제 후기 데이터로 덮어쓴다.
    """
    for wp in workplaces[:top_n]:
        review_data = search_workplace_reviews(wp["name"], region_name)
        wp["reviews"] = review_data.get("raw_review", "")
        for field in ("wifi", "outlet", "long_stay"):
            extracted = review_data.get(field)
            if extracted is not None:
                wp[field] = extracted  # 추정값 → 실제 후기값으로 보정
    for wp in workplaces[top_n:]:
        wp["reviews"] = ""
    return workplaces


# ── LLM 함수 ─────────────────────────────────────────────────────────

async def _extract_requirements_llm(user_input: UserInput) -> dict:
    """사용자 입력 → 필수/선호 조건 추출."""
    try:
        text = await call_llm(
            messages=[{"role": "user", "content": WORK_REQUIREMENTS_USER.format(
                purpose=user_input.purpose or "",
                work_required=user_input.work_required,
                work_style=user_input.work_style or "",
                transport=user_input.transport or "",
                companion=user_input.companion or "",
                desired_vibe=user_input.desired_vibe or "",
                additional_request=user_input.additional_request or "",
            )}],
            system=WORK_REQUIREMENTS_SYSTEM,
            max_tokens=512,
        )
        return json.loads(text)
    except Exception:
        return {"must_have": [], "prefer": []}


async def _evaluate_workplaces_llm(
    accommodation_id: str,
    user_input: UserInput,
    must_have: list[str],
    prefer: list[str],
    workplaces: list[dict],
) -> dict:
    """작업 공간 데이터 → 업무 환경 종합 평가."""
    try:
        try:
            ui_dict = user_input.model_dump()
        except AttributeError:
            ui_dict = user_input.dict()

        text = await call_llm(
            messages=[{"role": "user", "content": WORK_EVALUATE_USER.format(
                accommodation_id=accommodation_id,
                user_input_json=json.dumps(ui_dict, ensure_ascii=False, indent=2),
                must_have="\n".join(must_have) if must_have else "없음",
                prefer="\n".join(prefer) if prefer else "없음",
                workplaces_json=json.dumps(workplaces, ensure_ascii=False, indent=2),
            )}],
            system=WORK_EVALUATE_SYSTEM,
            max_tokens=2000,
        )
        return json.loads(text)
    except Exception:
        return {
            "status": "UNKNOWN",
            "total_score": 0.0,
            "confidence": 0.0,
            "summary": "평가 중 오류가 발생했습니다.",
            "details": {},
        }


# ── 메인 노드 함수 ────────────────────────────────────────────────────

async def work_agent(state: GraphState) -> dict:
    accommodations: list[dict] = state.get("candidate_accommodations", [])
    user_input: UserInput = state["user_input"]
    region_name: str = state.get("selected_region", {}).get("region_name", "")

    evaluations: list[WorkEvaluation] = []
    warnings: list[str] = []

    # 조건 추출은 한 번만 — 모든 숙소 평가에 공통 적용
    requirements = await _extract_requirements_llm(user_input)
    must_have: list[str] = requirements.get("must_have", [])
    prefer: list[str] = requirements.get("prefer", [])

    transport = user_input.transport
    by_car = is_car_transport(transport)

    for accommodation in accommodations:
        acc_id = str(accommodation.get("id", ""))
        longitude = accommodation.get("longitude")
        latitude = accommodation.get("latitude")

        if longitude is None or latitude is None:
            workplaces: list[dict] = []
        else:
            workplaces = search_workplaces(longitude, latitude, transport=transport)

            retry = 0
            while not workplaces and retry < RETRY_MAX_COUNT:
                retry += 1
                base = SEARCH_RADIUS_CAR_KM if by_car else SEARCH_RADIUS_WALK_KM
                expanded_radius = base + RETRY_RADIUS_EXPAND_KM
                workplaces = search_workplaces(
                    longitude, latitude,
                    transport=transport,
                    radius_km=expanded_radius,
                )
                warnings.append(f"[{acc_id}] 검색 결과 없어 반경 {expanded_radius}km로 확장 재시도.")

        if workplaces:
            workplaces = _enrich_with_reviews(workplaces, region_name=region_name)

        eval_result = await _evaluate_workplaces_llm(acc_id, user_input, must_have, prefer, workplaces)

        confidence = eval_result.get("confidence") or 0.0
        details = eval_result.get("details", {})
        details["status"] = eval_result.get("status", "UNKNOWN")
        evaluation = WorkEvaluation(
            accommodation_id=acc_id,
            score=eval_result.get("total_score", 0.0),
            confidence=confidence,
            summary=eval_result.get("summary", ""),
            details=details,
        )
        evaluations.append(evaluation)

        if confidence <= RETRY_CONFIDENCE_THRESHOLD:
            warnings.append(f"[{acc_id}] 신뢰도({confidence:.0f})가 낮아 추가 확인이 필요합니다.")

    result: dict = {"work_evaluations": evaluations}
    if warnings:
        result["warnings"] = warnings
    return result
