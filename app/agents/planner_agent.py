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
from typing import Optional

from app.agents.living_agent import living_agent
from app.agents.local_agent import evaluate_accommodations
from app.agents.stay_agent import accommodation_search_node, region_search_node
from app.agents.work_agent import work_agent
from app.core.llm import call_llm
from app.core.state import GraphState
from app.prompts.planner_prompts import FINAL_OUTPUT_SYSTEM, build_final_output_user
from app.schemas.output import FinalOutput


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
    """Stay Agent를 호출해 지역 후보를 반환한다.

    TODO: UserInput → parsed_preferences / must_have_conditions 등 5개 조건
          해석 LLM 호출을 여기에 추가 (현재는 호출자가 state에 미리 채워야 함).
    """
    return await region_search_node(state)


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


async def build_final_output(
    normalized: list[dict],
    work_evals: list,
    living_evals: list,
    local_evals: list,
    state: GraphState,
) -> FinalOutput:
    """세 Agent 평가 결과를 LLM에 전달해 최종 추천 순위를 생성한다."""
    accommodations_data = _assemble_accommodations_data(
        normalized, work_evals, living_evals, local_evals
    )

    user_msg = build_final_output_user(
        accommodations_data=accommodations_data,
        must_have_conditions=state.get("must_have_conditions") or [],
        priority_weights=state.get("priority_weights") or {},
        parsed_preferences=state.get("parsed_preferences") or {},
        selected_region=state.get("selected_region", {}).get("region_name", ""),
    )

    return await call_llm(
        messages=[{"role": "user", "content": user_msg}],
        system=FINAL_OUTPUT_SYSTEM,
        output_schema=FinalOutput,
    )


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
