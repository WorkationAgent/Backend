import uuid
from fastapi import APIRouter, HTTPException

from app.agents.planner_agent import (
    parse_raw_input, interpret_user_input,
    planner_phase2,
)
from app.agents.stay_agent import region_search_node
from app.api.schemas import (
    PlanRequest, PlanResponse, ParsedConditions, RegionCandidate,
    SelectRegionRequest, RecommendResponse, AccommodationResult,
    CategoryScores, EvaluatedItem, MapPoint,
)

router = APIRouter()

# 인메모리 세션 저장소
sessions: dict[str, dict] = {}


def _to_region_candidate(raw: dict, index: int) -> RegionCandidate:
    """백엔드 candidate_regions → 프론트 RegionCandidate 변환."""
    return RegionCandidate(
        id=raw.get("region_id", f"region_{index}"),
        name=raw.get("region_name", ""),
        living_area=raw.get("region_name", ""),
        description=raw.get("brief_reason", ""),
        tags=raw.get("area_type", []) + raw.get("characteristics", []),
        match_reasons=[raw.get("brief_reason", "")] if raw.get("brief_reason") else [],
        weaknesses=raw.get("possible_risks", []),
        is_best=(raw.get("rank") == 1),
    )


def _find_eval(eval_map: dict, acc_id: str, rank: int, evals: list):
    """ID로 찾고 없으면 rank 순서로 fallback."""
    found = eval_map.get(acc_id)
    if found:
        return found
    # rank는 1-based, evals는 0-based index
    idx = rank - 1
    if 0 <= idx < len(evals):
        return evals[idx]
    return None


def _to_accommodation_result(ranked: Any, work_map: dict, living_map: dict, local_map: dict,
                              work_evals: list = [], living_evals: list = [], local_evals: list = []) -> AccommodationResult:
    """RankedAccommodation + 에이전트 평가 → 프론트 AccommodationResult 변환."""
    acc_id = str(ranked.accommodation_id)
    rank = ranked.rank
    wk = _find_eval(work_map, acc_id, rank, work_evals)
    lv = _find_eval(living_map, acc_id, rank, living_evals)
    lc = _find_eval(local_map, acc_id, rank, local_evals)

    map_points = [
        MapPoint(
            name=p.name,
            category=p.category,
            latitude=p.latitude,
            longitude=p.longitude,
            description=p.description,
        )
        for p in (ranked.map_points or [])
    ]

    return AccommodationResult(
        rank=ranked.rank,
        overall_score=ranked.total_score,
        category_scores=CategoryScores(
            work=wk.score if wk else None,
            living=lv.score if lv else None,
            local=lc.score if lc else None,
        ),
        name=ranked.name,
        accommodation_id=acc_id,
        location_text=ranked.address,
        map_points=map_points,
        matched_conditions=[],
        work_summary=ranked.work_summary,
        living_summary=ranked.living_summary,
        local_summary=ranked.local_summary,
        work_environment=[EvaluatedItem(**item.model_dump()) for item in (ranked.work_environment or [])],
        living_elements=[EvaluatedItem(**item.model_dump()) for item in (ranked.living_elements or [])],
        local_experiences=[EvaluatedItem(**item.model_dump()) for item in (ranked.local_experiences or [])],
    )


@router.post("/plan", response_model=PlanResponse)
async def plan(body: PlanRequest):
    """줄글 입력 → 생활권 후보 3개 반환."""
    user_input = await parse_raw_input(body.text)
    interpreted = await interpret_user_input(user_input)

    state = {**interpreted, "user_input": user_input, "retry_count": {}, "errors": []}
    result = await region_search_node(state)

    thread_id = str(uuid.uuid4())
    sessions[thread_id] = {
        **state,
        "candidate_regions": result["candidate_regions"],
    }

    candidates = [
        _to_region_candidate(r, i)
        for i, r in enumerate(result["candidate_regions"])
    ]

    return PlanResponse(
        thread_id=thread_id,
        parsed=ParsedConditions(
            must_have=interpreted.get("must_have_conditions", []),
            preferences=interpreted.get("preference_conditions", []),
        ),
        candidate_regions=candidates,
    )


@router.post("/select-region", response_model=RecommendResponse)
async def select_region(body: SelectRegionRequest):
    """지역 선택 → 숙소 탐색 → 최종 추천 반환."""
    state = sessions.get(body.thread_id)
    if not state:
        raise HTTPException(status_code=404, detail="세션이 없거나 만료됐어요. 처음부터 다시 시작해주세요.")

    # region_id로 selected_region 찾기
    candidate_regions = state.get("candidate_regions", [])
    selected = next(
        (r for r in candidate_regions if r.get("region_id") == body.region_id),
        candidate_regions[0] if candidate_regions else None,
    )
    if not selected:
        raise HTTPException(status_code=400, detail="선택한 지역을 찾을 수 없어요.")

    state = {**state, "selected_region": selected}

    # Planner Phase 2 — 오케스트레이터가 워커 선택 및 최종 출력까지 담당
    result = await planner_phase2(state)

    final = result.get("final_user_output")
    if not final:
        errors = result.get("errors", [])
        raise HTTPException(status_code=500, detail=f"최종 추천 생성 실패: {errors}")

    work_evals   = result.get("work_evaluations", [])
    living_evals = result.get("living_evaluations", [])
    local_evals  = result.get("local_evaluations", [])

    work_map   = {str(e.accommodation_id): e for e in work_evals}
    living_map = {str(e.accommodation_id): e for e in living_evals}
    local_map  = {str(e.accommodation_id): e for e in local_evals}

    candidates = [
        _to_accommodation_result(acc, work_map, living_map, local_map,
                                  work_evals, living_evals, local_evals)
        for acc in final.ranked_accommodations
    ]

    sessions.pop(body.thread_id, None)

    return RecommendResponse(
        recommended_region=final.recommended_region or selected.get("region_name", ""),
        candidates=candidates,
    )
