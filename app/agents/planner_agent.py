"""
Planner — 파이프라인의 Planner 단계 로직 모음.

이 모듈은 더 이상 직접 오케스트레이션하지 않는다. 실제 흐름 제어는
LangGraph 그래프(app/graph/workflow.py)가 담당하고, 여기서는 각 그래프
노드가 호출하는 순수 함수만 제공한다:

  - parse_raw_input          : 줄글 → UserInput + excluded_regions
  - interpret_user_input     : UserInput → 조건 해석 + priority_weights
  - normalize_accommodations : Stay 출력 → 워커 공통 포맷
  - select_workers /
    build_skipped_agents     : 동적 워커 디스패치 결정
  - build_final_output       : 평가 통합 → 최종 추천 순위
"""

from __future__ import annotations

import json
from typing import Optional

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

async def parse_raw_input(raw_text: str) -> tuple[UserInput, list[str]]:
    """사용자 줄글 → (UserInput, excluded_regions) 구조화."""
    text = await call_llm(
        messages=[{"role": "user", "content": PARSE_RAW_USER.format(raw_text=raw_text)}],
        system=PARSE_RAW_SYSTEM,
        max_tokens=800,
    )
    data = json.loads(text)
    excluded_regions: list[str] = data.pop("excluded_regions", []) or []
    cleaned = {k: v for k, v in data.items() if v is not None and v != "null"}
    ui = UserInput(**cleaned)

    # 워케이션은 정의상 작업이 전제 — 명시적 거부가 없으면 work_required=true 보장.
    # (프롬프트 추론이 누락돼도 결정적으로 보정하는 안전망)
    if ui.work_required is None and ui.purpose and "워케이션" in ui.purpose:
        ui.work_required = True

    return ui, excluded_regions


async def interpret_user_input(user_input: UserInput) -> dict:
    """UserInput → 조건 해석 (parsed_preferences, must/avoid/preference_conditions, priority_weights)."""
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
    weights.pop("transport", None)  # transport는 living 내부 평가로 처리

    # ── 명시적 신호 보정 ────────────────────────────────────────
    # Work
    if user_input.work_required is False:
        weights["work"] = 0.0

    # Living — 단기(5일 이하)면 낮추기
    duration = (user_input.duration or "").lower()
    is_short = any(kw in duration for kw in ["1일", "2일", "3일", "4일", "5일", "2박", "3박", "4박"])
    if is_short:
        weights["living"] = min(weights.get("living", 0.25), 0.20)

    # 합이 1.0 되도록 재정규화 (부동소수점 오차는 최댓값에 흡수)
    total = sum(weights.values())
    if total > 0:
        weights = {k: round(v / total, 3) for k, v in weights.items()}
        diff = round(1.0 - sum(weights.values()), 3)
        if diff != 0:
            max_key = max(weights, key=weights.get)
            weights[max_key] = round(weights[max_key] + diff, 3)

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
            "accommodation_info": {
                "homepage":   acc.get("homepage"),
                "tel":        acc.get("tel"),
                "price": acc.get("price"),
            },
        })
    return result


# ── 동적 워커 디스패치 (오케스트레이터-워커) ────────────────────────────────

# priority_weights가 이 임계값을 초과하는 워커만 실행한다.
WORKER_WEIGHT_THRESHOLD = 0.05

# 가중치 미지정 시 기본값 (planner_phase2의 기존 동작 보존)
_WORKER_WEIGHT_DEFAULTS = {"work": 0.0, "living": 0.25, "local": 0.10}

_SKIP_REASONS = {
    "work":   "워케이션(원격근무) 요청이 아니어서 작업 환경은 평가하지 않았어요.",
    "living": "생활 인프라 우선순위가 낮아 평가하지 않았어요.",
    "local":  "관광·로컬 경험 우선순위가 낮아 평가하지 않았어요.",
}


def select_workers(priority_weights: dict) -> list[str]:
    """priority_weights 기준으로 실행할 워커 이름 목록(work/living/local)을 반환."""
    return [
        name
        for name, default in _WORKER_WEIGHT_DEFAULTS.items()
        if (priority_weights or {}).get(name, default) > WORKER_WEIGHT_THRESHOLD
    ]


def build_skipped_agents(priority_weights: dict) -> dict[str, str]:
    """실행하지 않는 워커의 사유 문구(프론트 섹션 안내용)."""
    active = set(select_workers(priority_weights))
    return {name: msg for name, msg in _SKIP_REASONS.items() if name not in active}


# ── 최종 출력 생성 ────────────────────────────────────────────────────────────

def _dump_details(details) -> dict:
    """Pydantic 모델 또는 dict 모두 안전하게 dict로 변환."""
    if details is None:
        return {}
    if hasattr(details, "model_dump"):
        return details.model_dump()
    if isinstance(details, dict):
        return details
    return {}


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
            "accommodation_info": acc.get("accommodation_info"),
            "work_eval": {
                "score":      w.score      if w else None,
                "confidence": w.confidence if w else None,
                "summary":    w.summary    if w else None,
                "details":    _dump_details(w.details) if w else {},
            },
            "living_eval": {
                "score":      l.score      if l else None,
                "confidence": l.confidence if l else None,
                "summary":    l.summary    if l else None,
                "details":    _dump_details(l.details) if l else {},
            },
            "local_eval": {
                "score":      lo.score      if lo else None,
                "confidence": lo.confidence if lo else None,
                "summary":    lo.summary    if lo else None,
                "details":    _dump_details(lo.details) if lo else {},
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
    return float(round(score))


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

    final: FinalOutput = await call_llm(
        messages=[{"role": "user", "content": user_msg}],
        system=FINAL_OUTPUT_SYSTEM,
        output_schema=FinalOutput,
        max_tokens=8192,   # 숙소 3개 × 풍부한 항목 → 4096이면 JSON이 잘려 파싱 실패
    )

    # LLM이 total_score를 임의로 생성하지 못하도록 코드 계산값으로 강제 덮어씌우기
    score_map = {acc["accommodation_id"]: acc["total_score"] for acc in accommodations_data}
    for ranked in final.ranked_accommodations:
        calculated = score_map.get(str(ranked.accommodation_id))
        if calculated is not None:
            ranked.total_score = float(round(calculated))

    # 점수 기준으로 재정렬 후 순위 재부여 (LLM 순위 무시)
    final.ranked_accommodations.sort(key=lambda x: x.total_score, reverse=True)
    for i, acc in enumerate(final.ranked_accommodations):
        acc.rank = i + 1

    return final


