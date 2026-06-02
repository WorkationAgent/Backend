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
from app.agents.local_agent import evaluate_accommodations
from app.agents.stay_agent import accommodation_search_node, region_search_node
from app.agents.work_agent import work_agent
from app.core.llm import call_llm
from app.core.state import GraphState
from app.prompts.planner_prompts import (
    INTERPRET_SYSTEM, INTERPRET_USER,
    PARSE_RAW_SYSTEM, PARSE_RAW_USER,
)
from app.schemas.user_input import UserInput


# ── Phase 1 헬퍼: UserInput → 5개 조건 해석 ──────────────────────────────────

async def parse_raw_input(raw_text: str) -> UserInput:
    """사용자 줄글 → UserInput 구조화."""
    text = await call_llm(
        messages=[{
            "role": "user",
            "content": PARSE_RAW_USER.format(raw_text=raw_text),
        }],
        system=PARSE_RAW_SYSTEM,
        max_tokens=800,
    )
    data = json.loads(text)
    # null 값 제거 후 UserInput 생성
    cleaned = {k: v for k, v in data.items() if v is not None and v != "null"}
    return UserInput(**cleaned)


async def interpret_user_input(user_input: UserInput) -> dict:
    """UserInput을 LLM으로 해석해 5개 조건을 반환한다.

    반환 키:
        parsed_preferences, must_have_conditions, avoid_conditions,
        preference_conditions, priority_weights
    """
    text = await call_llm(
        messages=[{
            "role": "user",
            "content": INTERPRET_USER.format(
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
            ),
        }],
        system=INTERPRET_SYSTEM,
        max_tokens=1500,
    )
    result = json.loads(text)
    return {
        "parsed_preferences":    result.get("parsed_preferences", {}),
        "must_have_conditions":  result.get("must_have_conditions", []),
        "avoid_conditions":      result.get("avoid_conditions", []),
        "preference_conditions": result.get("preference_conditions", []),
        "priority_weights":      result.get("priority_weights", {}),
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

    # 1. 줄글 → UserInput 구조화
    raw_text: str = state.get("raw_user_input", "")
    user_input = await parse_raw_input(raw_text)

    # 2. UserInput → 5개 조건 해석
    interpreted = await interpret_user_input(user_input)

    # state 갱신
    state = {**state, "user_input": user_input, **interpreted}

    # 3. Stay Agent Phase 1: 후보 생활권 3개 탐색
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

    # 3. Living / Work / Local 병렬 실행
    errors: list  = list(state.get("errors") or [])
    warnings: list = list(state.get("warnings") or [])
    retry_count   = dict(state.get("retry_count") or {})

    living_state = {**state, "candidate_accommodations": normalized}

    raw_results = await asyncio.gather(
        living_agent(living_state),
        _call_work(state, normalized),
        _call_local(state, normalized),
        return_exceptions=True,
    )

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

    # TODO: integrate_scores — 세 agent 점수를 가중 합산하여 ranked_recommendations 생성

    return {**merged, "errors": errors, "warnings": warnings, "retry_count": retry_count}


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

async def _call_work(state: GraphState, normalized: list[dict]) -> dict:
    result = await work_agent({**state, "candidate_accommodations": normalized})
    return {
        "work_evaluations": result.get("work_evaluations", []),
        "warnings":         result.get("warnings", []),
    }


async def _call_local(state: GraphState, normalized: list[dict]) -> dict:
    stay_dates = state.get("parsed_preferences", {}).get("stay_dates")
    evaluations = await evaluate_accommodations(
        accommodations=normalized,
        user_input=state["user_input"],
        stay_dates=stay_dates,
    )
    return {"local_evaluations": list(evaluations)}
