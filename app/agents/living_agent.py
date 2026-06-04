"""
Living Agent — 생활 인프라 평가.

실행 흐름:
  1. Quick Scan   : 첫 번째 숙소 좌표로 지역 인프라 현황 파악 (Kakao, 빠름)
  2. Planning LLM : scan 결과 + 사용자 선호도 → 지역 공통 검색 전략 (LivingSearchPlan)
  3. 숙소별 병렬  : Tool → Reflection LLM → (필요시 재탐색) → Evaluation LLM
  4. 재평가       : confidence ≤ RETRY_CONFIDENCE_THRESHOLD 숙소에 한해 1회 재실행
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Literal, Optional, Tuple

from app.config.settings import RETRY_CONFIDENCE_THRESHOLD, RETRY_MAX_COUNT, LLM_MODEL, LLM_MODEL_SONNET
from app.core.llm import call_llm
from app.core.state import GraphState
from app.prompts.living_prompts import EVALUATION_SYSTEM, PLANNING_SYSTEM, REFLECTION_SYSTEM
from app.schemas.living_schema import (
    LivingAssessment,
    LivingDetails,
    LivingSearchPlan,
    ReflectionResult,
)
from app.schemas.worker import LivingEvaluation
from app.tools.living_tool import (
    geocode_address,
    quick_scan,
    reverse_geocode,
    search_categories,
    search_living_infra,
)

_SCAN_RADIUS: Dict[str, float] = {"walk": 3.0, "car": 10.0}

# 단계별 모델 (Hybrid 전략)
_MODEL_PLANNING    = LLM_MODEL_SONNET  # 구조화 작업
_MODEL_REFLECTION  = LLM_MODEL        # 가짜 결과 감지 — 핵심 판단
_MODEL_EVALUATION  = LLM_MODEL_SONNET  # 명확한 기준 기반 평가


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def _transport_mode(parsed_preferences: Dict[str, Any]) -> Literal["walk", "car"]:
    """Planner가 parsed_preferences에 저장한 이동 방식 읽기. 기본값 walk."""
    val = str(parsed_preferences.get("transport") or "").lower()
    if any(k in val for k in ("자차", "자동차", "car", "drive")):
        return "car"
    return "walk"


def _acc_id(acc: Dict[str, Any]) -> str:
    return str(acc.get("id") or acc.get("accommodation_id") or acc.get("name") or "unknown")


async def _coordinates(acc: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """숙소 딕셔너리에서 좌표 추출. 없으면 주소 → 좌표 변환."""
    lat = acc.get("latitude") or acc.get("lat")
    lng = acc.get("longitude") or acc.get("lng") or acc.get("lon")
    if lat and lng:
        return float(lat), float(lng)
    address = acc.get("address") or acc.get("addr")
    if address:
        return await geocode_address(str(address))
    return None


# ── LLM 호출 ─────────────────────────────────────────────────────────────────

async def _plan_search(
    parsed_preferences: Dict[str, Any],
    priority_weights: Dict[str, float],
    transport_mode: str,
    scan_data: Dict[str, Any],
    retry_hint: Optional[str] = None,
) -> Optional[LivingSearchPlan]:
    """LLM Call 1 — quick_scan 결과 + 사용자 선호도 → 지역 공통 검색 전략."""
    content = (
        f"사전 탐색 결과 (Kakao, 직선 거리 기준):\n"
        f"{json.dumps(scan_data, ensure_ascii=False, indent=2)}\n\n"
        f"사용자 선호도:\n{json.dumps(parsed_preferences, ensure_ascii=False, indent=2)}\n\n"
        f"카테고리 우선순위:\n{json.dumps(priority_weights, ensure_ascii=False, indent=2)}\n\n"
        f"이동 방식: {transport_mode}"
    )
    if retry_hint:
        content += f"\n\n재탐색 안내: {retry_hint}"

    try:
        return await call_llm(
            messages=[{"role": "user", "content": content}],
            system=PLANNING_SYSTEM,
            output_schema=LivingSearchPlan,
            model=_MODEL_PLANNING,
        )
    except Exception:
        return None


async def _reflect(
    details: LivingDetails,
    plan: LivingSearchPlan,
    parsed_preferences: Dict[str, Any],
) -> Optional[ReflectionResult]:
    """LLM Call 2 — Tool 결과 검토 → 재탐색 필요 여부 및 수정 키워드 결정."""
    content = (
        f"탐색 결과:\n{json.dumps(details.model_dump(), ensure_ascii=False, indent=2)}\n\n"
        f"사용된 검색 전략:\n{json.dumps(plan.model_dump(), ensure_ascii=False, indent=2)}\n\n"
        f"사용자 선호도:\n{json.dumps(parsed_preferences, ensure_ascii=False, indent=2)}"
    )
    try:
        return await call_llm(
            messages=[{"role": "user", "content": content}],
            system=REFLECTION_SYSTEM,
            output_schema=ReflectionResult,
            model=_MODEL_REFLECTION,
        )
    except Exception:
        return None


async def _evaluate(
    acc_id: str,
    details: LivingDetails,
    plan: LivingSearchPlan,
    parsed_preferences: Dict[str, Any],
) -> Optional[LivingAssessment]:
    """LLM Call 3 — 수집 결과 → 점수·신뢰도·요약."""
    content = (
        f"숙소 ID: {acc_id}\n\n"
        f"생활 인프라 탐색 결과:\n"
        f"{json.dumps(details.model_dump(), ensure_ascii=False, indent=2)}\n\n"
        f"검색 전략 (가중치·우선순위):\n"
        f"{json.dumps(plan.model_dump(), ensure_ascii=False, indent=2)}\n\n"
        f"사용자 선호도:\n{json.dumps(parsed_preferences, ensure_ascii=False, indent=2)}"
    )
    try:
        return await call_llm(
            messages=[{"role": "user", "content": content}],
            system=EVALUATION_SYSTEM,
            output_schema=LivingAssessment,
            model=_MODEL_EVALUATION,
        )
    except Exception:
        return None


# ── 숙소 단위 처리 ────────────────────────────────────────────────────────────

async def _targeted_retry(
    details: LivingDetails,
    plan: LivingSearchPlan,
    reflection: ReflectionResult,
    lat: float,
    lng: float,
    transport_mode: str,
    area_name: str = "",
) -> LivingDetails:
    """Reflection이 지정한 카테고리만 수정된 키워드로 재탐색 후 결과 병합."""
    from app.schemas.living_schema import CategorySearchPlan

    # 수정된 plan 구성: reflection의 키워드로 해당 카테고리만 교체
    updated_plan = plan.model_copy(deep=True)
    for cat in reflection.retry_categories:
        new_keywords = reflection.retry_keywords.get(cat, [])
        if not new_keywords:
            continue
        original: CategorySearchPlan = getattr(updated_plan, cat)
        setattr(updated_plan, cat, original.model_copy(
            update={"naver_keywords": new_keywords, "kakao_keywords": new_keywords}
        ))

    retry_results = await search_categories(
        lat, lng, updated_plan, transport_mode,
        categories=reflection.retry_categories,
        use_retry_radius=True,
        area_name=area_name,
    )

    # 기존 결과에서 재탐색 카테고리만 교체
    merged = {
        "transport": details.transport,
        "grocery":   details.grocery,
        "medical":   details.medical,
        "services":  details.services,
    }
    for cat, result in retry_results.items():
        if result.found:  # 재탐색에서도 못 찾으면 기존 결과 유지
            merged[cat] = result

    return LivingDetails(
        transport=merged["transport"],
        grocery=merged["grocery"],
        medical=merged["medical"],
        services=merged["services"],
        weights_applied=details.weights_applied,
    )


async def _process(
    acc: Dict[str, Any],
    plan: LivingSearchPlan,
    transport_mode: str,
    parsed_preferences: Dict[str, Any],
) -> LivingEvaluation:
    """숙소 하나: Tool → Reflection → (재탐색) → Evaluation."""
    aid = _acc_id(acc)

    coords = await _coordinates(acc)
    if not coords:
        return LivingEvaluation(
            accommodation_id=aid,
            summary="좌표를 확인할 수 없어 생활 인프라 평가를 수행하지 못했습니다.",
        )

    lat, lng = coords
    area_name = await reverse_geocode(lat, lng)

    # 1. Tool: 정밀 탐색
    details: LivingDetails = await search_living_infra(lat, lng, plan, transport_mode, area_name)

    # 2. Reflection: 결과 검토 → 재탐색 여부 결정
    reflection = await _reflect(details, plan, parsed_preferences)
    if reflection and reflection.needs_retry and reflection.retry_categories:
        details = await _targeted_retry(details, plan, reflection, lat, lng, transport_mode, area_name)

    # 3. Evaluation
    assessment = await _evaluate(aid, details, plan, parsed_preferences)
    if not assessment:
        return LivingEvaluation(
            accommodation_id=aid,
            details=details.model_dump(),
        )

    return LivingEvaluation(
        accommodation_id=aid,
        score=assessment.score,
        confidence=assessment.confidence,
        summary=assessment.summary,
        details=details.model_dump(),
    )


# ── 메인 에이전트 ─────────────────────────────────────────────────────────────

async def living_agent(state: GraphState) -> GraphState:
    """
    생활 인프라 평가 에이전트.

    읽는 state : candidate_accommodations, parsed_preferences, priority_weights, retry_count
    쓰는 state : living_evaluations, retry_count, errors
    """
    parsed_preferences: Dict[str, Any] = state.get("parsed_preferences") or {}
    priority_weights: Dict[str, float] = state.get("priority_weights") or {}
    must_have_conditions: List[str]    = state.get("must_have_conditions") or []
    candidates: List[Dict[str, Any]]   = state.get("candidate_accommodations") or []
    retry_count: Dict[str, int]        = dict(state.get("retry_count") or {})
    errors: List[str]                  = list(state.get("errors") or [])

    # must_have_conditions를 parsed_preferences에 주입 (Planning LLM에 전달)
    if must_have_conditions:
        parsed_preferences = {**parsed_preferences, "must_have_conditions": must_have_conditions}

    if not candidates:
        return {**state, "living_evaluations": [], "errors": errors}

    transport_mode = _transport_mode(parsed_preferences)

    # ── 1. Quick Scan ─────────────────────────────────────────────────────────
    ref_coords = await _coordinates(candidates[0])
    if ref_coords:
        scan_data = await quick_scan(*ref_coords, _SCAN_RADIUS[transport_mode])
    else:
        scan_data = {"scan_radius_km": _SCAN_RADIUS[transport_mode]}

    # ── 2. Planning LLM ───────────────────────────────────────────────────────
    plan = await _plan_search(parsed_preferences, priority_weights, transport_mode, scan_data)
    if plan is None:
        errors.append("living_agent: Planning LLM 호출 실패")
        return {**state, "errors": errors}

    # ── 3. 숙소별 병렬: Tool → Reflection → Evaluate ──────────────────────────
    evaluations: List[LivingEvaluation] = list(
        await asyncio.gather(
            *[_process(acc, plan, transport_mode, parsed_preferences) for acc in candidates]
        )
    )

    # ── 4. 신뢰도 낮은 숙소 재평가 (1회 한정) ────────────────────────────────
    if retry_count.get("living", 0) < RETRY_MAX_COUNT:
        low = [
            e for e in evaluations
            if e.confidence is not None and e.confidence <= RETRY_CONFIDENCE_THRESHOLD
        ]
        if low:
            low_ids = {e.accommodation_id for e in low}
            retry_candidates = [acc for acc in candidates if _acc_id(acc) in low_ids]

            retry_plan = await _plan_search(
                parsed_preferences,
                priority_weights,
                transport_mode,
                scan_data,
                retry_hint="이전 탐색의 신뢰도가 낮습니다. 더 다양한 키워드와 넓은 범위로 전략을 수정하세요.",
            )
            if retry_plan:
                retry_evals: List[LivingEvaluation] = list(
                    await asyncio.gather(
                        *[_process(acc, retry_plan, transport_mode, parsed_preferences)
                          for acc in retry_candidates]
                    )
                )
                retry_map = {e.accommodation_id: e for e in retry_evals}
                evaluations = [retry_map.get(e.accommodation_id, e) for e in evaluations]
                retry_count["living"] = retry_count.get("living", 0) + 1

    return {
        **state,
        "living_evaluations": evaluations,
        "retry_count": retry_count,
        "errors": errors,
    }
