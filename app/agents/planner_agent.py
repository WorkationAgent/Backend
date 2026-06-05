"""
Planner Agent — 전체 파이프라인을 조율하는 Supervisor Agent.

Phase 1 (planner_phase1):
    UserInput → 5개 조건 해석 (미구현, 호출자가 state에 미리 채워야 함)
    → Stay Agent: 지역 후보 3개 검색
    쓰기: candidate_regions

Phase 2 (planner_phase2):  (사용자 지역 선택 후 재개)
    → Stay Agent: 숙소 3개 검색
    → normalize_accommodations: mapx/mapy → lat/lng 변환
    → Living / Work / Local Agent 병렬 실행
    → integrate_scores: 점수 통합 및 순위 (미구현)
    쓰기: candidate_accommodations, normalized_accommodations,
          living_evaluations, work_evaluations, local_evaluations,
          errors, warnings, retry_count
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from app.agents.living_agent import living_agent
from app.agents.local_agent import local_agent, evaluate_accommodations
from app.agents.stay_agent import accommodation_search_node, region_search_node
from app.agents.work_agent import work_agent
from app.core.llm import call_llm
from app.core.state import GraphState
from app.prompts.planner_prompts import (
    FINAL_OUTPUT_SYSTEM, build_final_output_user,
    INTERPRET_SYSTEM, INTERPRET_USER,
    PARSE_RAW_SYSTEM, PARSE_RAW_USER,
)
from app.schemas.output import FinalOutput
from app.schemas.user_input import UserInput


# ── 줄글 파싱 & 해석 ──────────────────────────────────────────────────────────

async def parse_raw_input(raw_text: str) -> UserInput:
    """사용자 줄글 → UserInput 구조화."""
    text = await call_llm(
        messages=[{"role": "user", "content": PARSE_RAW_USER.format(raw_text=raw_text)}],
        system=PARSE_RAW_SYSTEM,
        max_tokens=800,
    )
    data = json.loads(text)
    cleaned = {k: v for k, v in data.items() if v is not None and v != "null"}
    return UserInput(**cleaned)


async def interpret_user_input(user_input: UserInput) -> dict:
    """UserInput → 5개 조건 해석 (parsed_preferences, must/avoid/preference_conditions, priority_weights)."""
    text = await call_llm(
        messages=[{"role": "user", "content": INTERPRET_USER.format(
            purpose=user_input.purpose or "미입력",
            duration=user_input.duration or "미입력",
            desired_region=user_input.desired_region or "미입력",
            region_style=user_input.region_style or "미입력",
            desired_vibe=user_input.desired_vibe or "미입력",
            tourism_hobby=user_input.tourism_hobby or "미입력",
            work_required=user_input.work_required,
            work_style=user_input.work_style or "미입력",
            transport=user_input.transport or "미입력",
            travel_distance=user_input.travel_distance or "미입력",
            living_infra=user_input.living_infra or "미입력",
            budget=user_input.budget or "미입력",
            accommodation_style=user_input.accommodation_style or "미입력",
            companion=user_input.companion or "미입력",
            priority=user_input.priority or "미입력",
            additional_request=user_input.additional_request or "미입력",
        )}],
        system=INTERPRET_SYSTEM,
        max_tokens=1500,
    )
    result = json.loads(text)
    weights = result.get("priority_weights", {})

    # ── 명시적 신호 보정 ────────────────────────────────────────
    # Work
    if user_input.work_required is False or user_input.work_required is None:
        weights["work"] = 0.0

    # Local — tourism_hobby 없고 휴식형이면 0
    if not user_input.tourism_hobby:
        purpose = (user_input.purpose or "").lower()
        if any(kw in purpose for kw in ["휴식", "쉬", "촌캉스", "rest", "vacation"]):
            weights["local"] = 0.0
        else:
            weights["local"] = min(weights.get("local", 0.1), 0.05)

    # Living — 단기(5일 이하)면 낮추기
    duration = (user_input.duration or "").lower()
    is_short = any(kw in duration for kw in ["1일", "2일", "3일", "4일", "5일", "2박", "3박", "4박"])
    if is_short:
        weights["living"] = min(weights.get("living", 0.25), 0.20)

    # 합이 1.0 되도록 재정규화
    total = sum(weights.values())
    if total > 0:
        weights = {k: round(v / total, 3) for k, v in weights.items()}

    return {
        "parsed_preferences":    result.get("parsed_preferences", {}),
        "must_have_conditions":  result.get("must_have_conditions", []),
        "avoid_conditions":      result.get("avoid_conditions", []),
        "preference_conditions": result.get("preference_conditions", []),
        "priority_weights":      weights,
    }


# ── 정규화 ────────────────────────────────────────────────────────────────────

def normalize_accommodations(stay_output: list[dict]) -> list[dict]:
    """Stay Agent 출력 → worker 공통 포맷 변환.

    mapx(경도 문자열) → longitude(float)
    mapy(위도 문자열) → latitude(float)
    total_score      → stay_score
    brief_reason     → stay_reason
    나머지 필드 pass-through. 좌표 변환 실패 시 None.
    """
    result = []
    for acc in stay_output:
        mapx = acc.get("mapx")
        mapy = acc.get("mapy")

        try:
            longitude: Optional[float] = float(mapx) if mapx is not None else None
        except (TypeError, ValueError):
            longitude = None

        try:
            latitude: Optional[float] = float(mapy) if mapy is not None else None
        except (TypeError, ValueError):
            latitude = None

        result.append({
            "id":              acc.get("id", ""),
            "rank":            acc.get("rank"),
            "name":            acc.get("name", ""),
            "address":         acc.get("address"),
            "latitude":        latitude,
            "longitude":       longitude,
            "stay_score":      acc.get("total_score"),
            "stay_reason":     acc.get("brief_reason"),
            "score_breakdown": acc.get("score_breakdown", {}),
            "image_url":       acc.get("image_url"),
            "homepage":        acc.get("homepage"),
            "tel":             acc.get("tel"),
        })
    return result


# ── Phase 1: 지역 검색 ────────────────────────────────────────────────────────

async def planner_phase1(state: GraphState) -> dict:
    """줄글 입력 → 구조화 → 5개 조건 해석 → Stay Agent 지역 후보 탐색."""
    raw_text: str = state.get("raw_user_input", "")
    user_input = await parse_raw_input(raw_text)
    interpreted = await interpret_user_input(user_input)
    state = {**state, "user_input": user_input, **interpreted}
    region_result = await region_search_node(state)
    return {"user_input": user_input, **interpreted, **region_result}


# ── Phase 2: 숙소 검색 → 정규화 → 평가 ──────────────────────────────────────

async def planner_phase2(state: GraphState) -> dict:
    """숙소 검색부터 최종 평가까지 전체를 조율한다."""

    # 1. Stay Agent: 숙소 검색
    acc_result = await accommodation_search_node(state)
    state = {**state, **acc_result}

    # 2. 정규화: mapx/mapy → latitude/longitude
    raw = state.get("candidate_accommodations") or []
    normalized = normalize_accommodations(raw)
    state = {**state, "normalized_accommodations": normalized}

    # 3. 동적 워커 선택 (오케스트레이터-워커)
    errors: list  = list(state.get("errors") or [])
    warnings: list = list(state.get("warnings") or [])
    retry_count   = dict(state.get("retry_count") or {})

    user_input = state.get("user_input")
    priority_weights = state.get("priority_weights", {})

    run_work   = getattr(user_input, "work_required", None) is True
    run_living = priority_weights.get("living", 0.25) > 0.05
    run_local  = priority_weights.get("local", 0.10) > 0.05

    worker_state = {**state, "candidate_accommodations": normalized}

    coros = []
    if run_living: coros.append(living_agent(worker_state))
    if run_work:   coros.append(work_agent(worker_state))
    if run_local:  coros.append(local_agent(worker_state))

    raw_results = await asyncio.gather(*coros, return_exceptions=True)

    merged: dict = {
        "candidate_accommodations":  raw,
        "normalized_accommodations": normalized,
    }
    for raw_r in raw_results:
        if isinstance(raw_r, Exception):
            errors.append(str(raw_r))
            continue
        errors.extend(raw_r.get("errors") or [])
        warnings.extend(raw_r.get("warnings") or [])
        for k, v in (raw_r.get("retry_count") or {}).items():
            retry_count[k] = v
        for k, v in raw_r.items():
            if k not in ("errors", "warnings", "retry_count",
                         "candidate_accommodations"):
                merged[k] = v

    # 4. 최종 추천 순위 생성
    try:
        final_output = await build_final_output(
            normalized=normalized,
            work_evals=merged.get("work_evaluations", []),
            living_evals=merged.get("living_evaluations", []),
            local_evals=merged.get("local_evaluations", []),
            state=state,
        )
        merged["final_user_output"] = final_output
    except Exception as e:
        errors.append(f"planner: build_final_output 실패 — {e}")

    return {**merged, "errors": errors, "warnings": warnings, "retry_count": retry_count}


# ── 최종 출력 생성 ────────────────────────────────────────────────────────────

def _assemble_accommodations_data(
    normalized: list[dict],
    work_evals: list,
    living_evals: list,
    local_evals: list,
) -> list[dict]:
    """accommodation_id 기준으로 평가 결과를 숙소 정보에 합산."""
    work_map   = {e.accommodation_id: e for e in work_evals}
    living_map = {e.accommodation_id: e for e in living_evals}
    local_map  = {e.accommodation_id: e for e in local_evals}

    result = []
    for acc in normalized:
        acc_id = acc.get("id", "")
        w = work_map.get(acc_id)
        l = living_map.get(acc_id)
        lo = local_map.get(acc_id)

        result.append({
            "accommodation_id": acc_id,
            "name":             acc.get("name", ""),
            "address":          acc.get("address"),
            "latitude":         acc.get("latitude"),
            "longitude":        acc.get("longitude"),
            "stay_score":       acc.get("stay_score"),
            "stay_reason":      acc.get("stay_reason"),
            "homepage":         acc.get("homepage"),
            "tel":              acc.get("tel"),
            "work_eval": {
                "score":      w.score      if w else None,
                "confidence": w.confidence if w else None,
                "summary":    w.summary    if w else None,
                "details":    w.details    if w else {},
            },
            "living_eval": {
                "score":      l.score      if l else None,
                "confidence": l.confidence if l else None,
                "summary":    l.summary    if l else None,
                "details":    l.details    if l else {},
            },
            "local_eval": {
                "score":      lo.score      if lo else None,
                "confidence": lo.confidence if lo else None,
                "summary":    lo.summary    if lo else None,
                "details":    lo.details.model_dump() if lo and lo.details else {},
            },
        })
    return result


def _calculate_final_score(
    work_score: float | None,
    living_score: float | None,
    local_score: float | None,
    stay_score: float | None,
    priority_weights: dict,
) -> float:
    """priority_weights 기반 코드 가중 평균 (LLM 대신 결정적 계산)."""
    score = 0.0
    if work_score is not None:
        score += work_score   * priority_weights.get("work", 0)
    if living_score is not None:
        score += living_score * priority_weights.get("living", 0)
    if local_score is not None:
        score += local_score  * priority_weights.get("local", 0)
    if stay_score is not None:
        score += stay_score   * priority_weights.get("accommodation", 0)
    return round(score, 1)


async def build_final_output(
    normalized: list[dict],
    work_evals: list,
    living_evals: list,
    local_evals: list,
    state: GraphState,
) -> FinalOutput:
    """에이전트 평가 결과를 종합해 최종 추천 순위를 생성한다.

    total_score: 코드 기반 가중 평균 (priority_weights 사용)
    순위·정성 요약: LLM 생성
    """
    priority_weights = state.get("priority_weights") or {}
    accommodations_data = _assemble_accommodations_data(
        normalized, work_evals, living_evals, local_evals
    )

    # 코드로 total_score 계산 후 data에 주입
    for acc in accommodations_data:
        acc["total_score"] = _calculate_final_score(
            work_score   = acc["work_eval"].get("score"),
            living_score = acc["living_eval"].get("score"),
            local_score  = acc["local_eval"].get("score"),
            stay_score   = acc.get("stay_score"),
            priority_weights=priority_weights,
        )

    # total_score 기준 정렬 후 rank 부여
    accommodations_data.sort(key=lambda x: x["total_score"], reverse=True)
    for i, acc in enumerate(accommodations_data):
        acc["rank"] = i + 1

    user_msg = build_final_output_user(
        accommodations_data=accommodations_data,
        must_have_conditions=state.get("must_have_conditions") or [],
        priority_weights=priority_weights,
        parsed_preferences=state.get("parsed_preferences") or {},
        selected_region=state.get("selected_region", {}).get("region_name", ""),
    )

    return await call_llm(
        messages=[{"role": "user", "content": user_msg}],
        system=FINAL_OUTPUT_SYSTEM,
        output_schema=FinalOutput,
    )


