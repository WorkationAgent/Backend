"""
Work Agent (업무 환경 평가 에이전트)

State 입출력:
    읽기: state["candidate_accommodations"]  (숙소 후보)
          state["parsed_preferences"]        (Planner가 해석한 조건)
          state["must_have_conditions"]      (Planner가 추출한 필수 조건)
          state["preference_conditions"]     (Planner가 추출한 선호 조건)
          state["user_input"]                (work_style 동적 키워드용)
          state["selected_region"]           (선택된 생활권, region_name 추출용)
    쓰기: state["work_evaluations"]          (숙소별 평가 결과 + map_points)
          state["warnings"]                  (재시도/오류 경고)
          state["retry_count"]               (재시도 발생 시)

흐름:
    1. LLM으로 사용자 조건 기반 검색 키워드 생성 (1회)
    2. 숙소 3개 병렬 처리:
       a. place_tool로 주변 작업 공간 검색 (이동수단·키워드 기반)
       b. 결과 없거나 신뢰도 ≤ 54면 반경 확장 1회 재시도
       c. search_tool로 상위 3곳 네이버 후기 수집 및 추정값 보정
       d. LLM으로 업무 환경 종합 평가 (parsed_preferences 사용)
       e. PASS/CONDITIONAL_PASS일 때만 places + map_points 구성
"""

from __future__ import annotations

import asyncio
import json
import logging

from app.core.llm import call_llm
from app.config.settings import (
    SEARCH_RADIUS_CAR_KM,
    SEARCH_RADIUS_WALK_KM,
    RETRY_CONFIDENCE_THRESHOLD,
)
from app.core.state import GraphState
from app.prompts.work_prompts import (
    WORK_EVALUATE_SYSTEM,
    WORK_EVALUATE_USER,
    WORK_KEYWORDS_SYSTEM,
    WORK_KEYWORDS_USER,
)
from app.schemas.user_input import UserInput
from app.schemas.worker import WorkEvaluation
from app.tools.place_tool import is_car_transport, search_workplaces
from app.tools.search_tool import search_workplace_reviews

logger = logging.getLogger(__name__)


# ── 유틸 ──────────────────────────────────────────────────────────────

def _enrich_with_reviews(
    workplaces: list[dict],
    region_name: str = "",
    top_n: int = 3,
) -> list[dict]:
    """가장 가까운 top_n개 작업 공간에 네이버 후기를 추가하고 추정값을 보정한다."""
    for wp in workplaces[:top_n]:
        review_data = search_workplace_reviews(wp["name"], region_name)
        wp["reviews"] = review_data.get("raw_review", "")
        for field in ("wifi", "outlet", "long_stay", "quiet"):
            extracted = review_data.get(field)
            if extracted is not None:
                wp[field] = extracted
    for wp in workplaces[top_n:]:
        wp["reviews"] = ""
    return workplaces


# ── LLM 함수 ──────────────────────────────────────────────────────────

async def _generate_search_keywords_llm(
    work_style: str | None,
    must_have: list[str],
) -> list[str]:
    """사용자 작업 조건 → 카카오 검색 키워드 리스트 생성."""
    _DEFAULT = ["카페", "스터디카페", "공유오피스", "도서관"]
    try:
        text = await call_llm(
            messages=[{"role": "user", "content": WORK_KEYWORDS_USER.format(
                work_style=work_style or "미지정",
                must_have="\n".join(must_have) if must_have else "없음",
            )}],
            system=WORK_KEYWORDS_SYSTEM,
            max_tokens=200,
        )
        result = json.loads(text)
        keywords = result.get("keywords", [])
        return keywords if keywords else _DEFAULT
    except Exception as e:
        logger.warning(f"키워드 생성 LLM 실패: {e}")
        return _DEFAULT


async def _evaluate_workplaces_llm(
    accommodation_id: str,
    parsed_preferences: dict,
    must_have: list[str],
    prefer: list[str],
    workplaces: list[dict],
) -> dict:
    """작업 공간 데이터 → 업무 환경 종합 평가."""
    try:
        text = await call_llm(
            messages=[{"role": "user", "content": WORK_EVALUATE_USER.format(
                accommodation_id=accommodation_id,
                parsed_preferences_json=json.dumps(parsed_preferences, ensure_ascii=False, indent=2),
                must_have="\n".join(must_have) if must_have else "없음",
                prefer="\n".join(prefer) if prefer else "없음",
                workplaces_json=json.dumps(workplaces, ensure_ascii=False, indent=2),
            )}],
            system=WORK_EVALUATE_SYSTEM,
            max_tokens=2000,
        )
        return json.loads(text)
    except Exception as e:
        logger.warning(f"[{accommodation_id}] 평가 LLM 실패: {e}")
        return {
            "status": "FAIL",
            "total_score": 0.0,
            "confidence": 0.0,
            "summary": "평가 중 오류가 발생했습니다.",
            "details": {},
        }


# ── 숙소 단위 처리 ────────────────────────────────────────────────────

async def _process_one_accommodation(
    accommodation: dict,
    parsed_preferences: dict,
    must_have: list[str],
    prefer: list[str],
    region_name: str,
    search_keywords: list[str],
    transport_str: str,
    by_car: bool,
) -> tuple[WorkEvaluation, list[str], bool]:
    """숙소 1개 처리. 반환: (WorkEvaluation, warnings, retried)"""
    acc_id = str(accommodation.get("id", ""))
    longitude = accommodation.get("longitude")
    latitude = accommodation.get("latitude")
    acc_warnings: list[str] = []
    retried = False

    if longitude is None or latitude is None:
        workplaces: list[dict] = []
    else:
        workplaces = search_workplaces(
            longitude, latitude,
            transport=transport_str,
            keywords=search_keywords,
        )

    if workplaces:
        workplaces = _enrich_with_reviews(workplaces, region_name=region_name)

    eval_result = await _evaluate_workplaces_llm(
        acc_id, parsed_preferences, must_have, prefer, workplaces
    )
    confidence = eval_result.get("confidence") or 0.0

    # 결과 0개 또는 신뢰도 ≤ 54 → 반경 확장 1회 재시도 (도보 2배, 자차 1.5배)
    needs_retry = (not workplaces) or (confidence <= RETRY_CONFIDENCE_THRESHOLD)
    if needs_retry and longitude is not None and latitude is not None:
        expanded = SEARCH_RADIUS_CAR_KM * 1.5 if by_car else SEARCH_RADIUS_WALK_KM * 2.0
        retry_workplaces = search_workplaces(
            longitude, latitude,
            transport=transport_str,
            radius_km=expanded,
            keywords=search_keywords,
        )
        if retry_workplaces:
            retry_workplaces = _enrich_with_reviews(retry_workplaces, region_name=region_name)
            eval_result = await _evaluate_workplaces_llm(
                acc_id, parsed_preferences, must_have, prefer, retry_workplaces
            )
            confidence = eval_result.get("confidence") or 0.0
            workplaces = retry_workplaces
        retried = True
        acc_warnings.append(
            f"[{acc_id}] 반경 {expanded:.1f}km 확장 재시도 (결과부족 또는 신뢰도 {confidence:.0f})."
        )

    # FAIL이면 score 강제 0
    final_status = eval_result.get("status", "FAIL")
    total_score = eval_result.get("total_score", 0.0)
    if final_status == "FAIL":
        total_score = 0.0

    # PASS/CONDITIONAL_PASS일 때만 장소 노출 (FAIL이면 엉뚱한 장소 노출 방지)
    if final_status in ("PASS", "CONDITIONAL_PASS"):
        places_for_list = [
            {"name": wp["name"], "distance_min": wp.get("distance_min", 0), "type": wp.get("type", "")}
            for wp in workplaces[:5]
        ]
        map_points = [
            {"name": wp["name"], "lat": wp.get("lat"), "lng": wp.get("lng"), "type": wp.get("type", "")}
            for wp in workplaces[:5]
            if wp.get("lat") and wp.get("lng")
        ]
    else:
        places_for_list = []
        map_points = []

    details = eval_result.get("details", {})
    details["status"] = final_status
    details["places"] = places_for_list

    evaluation = WorkEvaluation(
        accommodation_id=acc_id,
        score=total_score,
        confidence=confidence,
        summary=eval_result.get("summary", ""),
        details=details,
        map_points=map_points,
    )
    return evaluation, acc_warnings, retried


# ── 메인 노드 함수 ────────────────────────────────────────────────────

async def work_agent(state: GraphState) -> dict:
    accommodations: list[dict] = state.get("candidate_accommodations", [])
    parsed_preferences: dict = state.get("parsed_preferences") or {}
    must_have: list[str] = state.get("must_have_conditions") or []
    prefer: list[str] = state.get("preference_conditions") or []
    region_name: str = state.get("selected_region", {}).get("region_name", "")

    user_input: UserInput = state["user_input"]
    transport_str = str(parsed_preferences.get("transport") or user_input.transport or "")
    by_car = is_car_transport(transport_str)

    # LLM으로 검색 키워드 생성 (숙소 루프 전 1회)
    search_keywords = await _generate_search_keywords_llm(user_input.work_style, must_have)

    # 숙소 병렬 처리
    results = await asyncio.gather(*[
        _process_one_accommodation(
            acc, parsed_preferences, must_have, prefer,
            region_name, search_keywords, transport_str, by_car,
        )
        for acc in accommodations
    ])

    evaluations: list[WorkEvaluation] = []
    warnings: list[str] = []
    any_retried = False

    for evaluation, acc_warnings, retried in results:
        evaluations.append(evaluation)
        warnings.extend(acc_warnings)
        if retried:
            any_retried = True

    result: dict = {
        "work_evaluations": evaluations,
        "warnings": warnings,
    }
    if any_retried:
        result["retry_count"] = {"work": 1}
    return result
