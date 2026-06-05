import asyncio
import uuid
from typing import Any
from fastapi import APIRouter, HTTPException

from app.agents.planner_agent import (
    parse_raw_input, interpret_user_input,
    planner_phase2,
)
from app.agents.stay_agent import region_search_node
from app.tools import kto
from app.api.schemas import (
    PlanRequest, PlanResponse, ParsedConditions, RegionCandidate,
    SelectRegionRequest, RecommendResponse, AccommodationResult,
    CategoryScores, EvaluatedItem, EvaluationSection, MapPoint,
    AccommodationInfo,
)

router = APIRouter()

# 인메모리 세션 저장소
sessions: dict[str, dict] = {}


def _map_point_kind(category: str, source: str | None = None) -> str:
    """백엔드 category/source → 프론트 kind 변환."""
    if category == "stay":
        return "stay"
    if category == "work" or source in ("work",):
        return "work"
    if source in ("living",):
        return "living"
    if source in ("local",):
        return "local"
    if category == "infra":
        return "living"
    if category == "experience":
        return "local"
    return "living"


def _to_region_candidate(raw: dict, index: int) -> RegionCandidate:
    brief = raw.get("brief_reason", "")
    return RegionCandidate(
        id=raw.get("region_id", f"region_{index}"),
        name=raw.get("region_name", ""),
        living_area=raw.get("region_name", ""),
        description=brief,
        tags=raw.get("area_type", []) + raw.get("characteristics", []),
        match_summary=brief,
        weaknesses=raw.get("possible_risks", []),
        is_best=(raw.get("rank") == 1),
    )


def _max_search_radius_m(work_eval, living_eval, local_eval) -> float | None:
    """세 에이전트가 사용한 검색 반경 중 최댓값(m). 없으면 None.

    - local : details.search_radius_used_km (typed, km)
    - living: details[cat].zone_km(발견 반경) + places[].distance_meters
    - work  : details의 반경 키가 있으면 방어적으로 포함
    """
    radii: list[float] = []

    # local (typed)
    det = getattr(local_eval, "details", None)
    km = getattr(det, "search_radius_used_km", 0) or 0
    if km:
        radii.append(float(km) * 1000)

    # living (details = LivingDetails.model_dump() dict)
    ld = getattr(living_eval, "details", None)
    if isinstance(ld, dict):
        for cat in ("transport", "grocery", "medical", "services"):
            c = ld.get(cat) or {}
            if not isinstance(c, dict):
                continue
            zk = c.get("zone_km")
            if zk:
                radii.append(float(zk) * 1000)
            for p in (c.get("places") or []):
                dm = (p or {}).get("distance_meters")
                if dm:
                    radii.append(float(dm))

    # work (details dict — 반경 키가 있을 때만)
    wd = getattr(work_eval, "details", None)
    if isinstance(wd, dict):
        for key in ("search_radius_km", "radius_km", "search_radius_used_km"):
            v = wd.get(key)
            if v:
                radii.append(float(v) * 1000)

    return round(max(radii)) if radii else None


def _to_evaluated_items(items: list) -> list[EvaluatedItem]:
    result = []
    for item in (items or []):
        try:
            d = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        except Exception:
            d = {}
        result.append(EvaluatedItem(
            name=d.get("name", ""),
            sub=d.get("description", d.get("sub", "")),
            distance_text=d.get("distance_text") or None,
        ))
    return result


def _to_accommodation_result(ranked: Any, work_eval, living_eval, local_eval,
                              coord_map: dict | None = None) -> AccommodationResult:
    # 좌표 — normalized_accommodations에서 acc_id로 조회
    acc_id = str(ranked.accommodation_id)
    coords = (coord_map or {}).get(acc_id, {})
    lat = float(coords.get("latitude") or 0.0)
    lng = float(coords.get("longitude") or 0.0)

    # MapPoints: 백엔드 MapPoint → 프론트 MapPoint
    map_points = []
    for p in (ranked.map_points or []):
        try:
            map_points.append(MapPoint(
                name=p.name,
                kind=_map_point_kind(p.category, getattr(p, "source", None)),
                lat=p.latitude,
                lng=p.longitude,
                description=p.description,
            ))
        except Exception:
            pass

    # sections — 스킵된 에이전트도 빈 섹션으로 항상 포함 (프론트 undefined 방지)
    sections: dict[str, EvaluationSection] = {
        "work":   EvaluationSection(score=0.0, summary="", items=[]),
        "living": EvaluationSection(score=0.0, summary="", items=[]),
        "local":  EvaluationSection(score=0.0, summary="", items=[]),
    }

    if work_eval:
        sections["work"] = EvaluationSection(
            score=round(work_eval.score or 0),
            summary=ranked.work_summary or "",
            items=_to_evaluated_items(ranked.work_environment),
        )

    if living_eval:
        sections["living"] = EvaluationSection(
            score=round(living_eval.score or 0),
            summary=ranked.living_summary or "",
            items=_to_evaluated_items(ranked.living_elements),
        )

    if local_eval:
        sections["local"] = EvaluationSection(
            score=round(local_eval.score or 0),
            summary=ranked.local_summary or "",
            items=_to_evaluated_items(ranked.local_experiences),
        )

    # 숙소 기본정보 — 값이 하나라도 있을 때만 포함 (price는 현재 데이터에 없어 None)
    homepage = getattr(ranked, "homepage", None)
    tel = getattr(ranked, "tel", None)
    acc_info = (
        AccommodationInfo(phone=tel, homepage=homepage)
        if (homepage or tel)
        else None
    )

    return AccommodationResult(
        rank=ranked.rank,
        overall_score=round(ranked.total_score or 0),
        name=ranked.name,
        address=ranked.address or "",
        center={"lat": lat, "lng": lng},
        search_radius_m=_max_search_radius_m(work_eval, living_eval, local_eval),
        matched_conditions=list(getattr(ranked, "matched_conditions", []) or []),
        map_points=map_points,
        category_scores=CategoryScores(
            work=round(work_eval.score or 0) if work_eval else 0.0,
            living=round(living_eval.score or 0) if living_eval else 0.0,
            local=round(local_eval.score or 0) if local_eval else 0.0,
        ),
        sections=sections,
        accommodation_info=acc_info,
    )


@router.post("/plan", response_model=PlanResponse)
async def plan(body: PlanRequest):
    """줄글 입력 → 생활권 후보 3개 반환."""
    user_input, excluded_regions = await parse_raw_input(body.text)
    interpreted = await interpret_user_input(user_input)

    state = {**interpreted, "user_input": user_input,
             "excluded_regions": excluded_regions, "retry_count": {}, "errors": []}
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

    # 지역 대표 사진(KTO) 병렬 조회 → photo_url 채움 (실패/없음은 None → 프론트 그라데이션 폴백)
    photos = await asyncio.gather(
        *[kto.search_region_image(c.name) for c in candidates],
        return_exceptions=True,
    )
    for c, img in zip(candidates, photos):
        if isinstance(img, str) and img:
            c.photo_url = img

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
        no_acc = any("숙소" in e for e in errors)
        detail = "해당 지역에서 숙소를 찾지 못했습니다. 다른 지역을 선택해주세요." if no_acc \
                 else "추천 결과를 생성하지 못했습니다. 다시 시도해주세요."
        raise HTTPException(status_code=404, detail=detail)

    work_evals   = result.get("work_evaluations", [])
    living_evals = result.get("living_evaluations", [])
    local_evals  = result.get("local_evaluations", [])

    work_map   = {str(e.accommodation_id): e for e in work_evals}
    living_map = {str(e.accommodation_id): e for e in living_evals}
    local_map  = {str(e.accommodation_id): e for e in local_evals}

    # 좌표 맵 — candidate_accommodations(normalized)에서 추출
    normalized = result.get("candidate_accommodations", [])
    coord_map  = {str(a.get("id", "")): a for a in normalized}

    def _find(m, acc_id, rank, evals):
        found = m.get(acc_id)
        if found:
            return found
        idx = rank - 1
        return evals[idx] if 0 <= idx < len(evals) else None

    candidates = []
    for acc in final.ranked_accommodations:
        acc_id = str(acc.accommodation_id)
        wk = _find(work_map,   acc_id, acc.rank, work_evals)
        lv = _find(living_map, acc_id, acc.rank, living_evals)
        lc = _find(local_map,  acc_id, acc.rank, local_evals)
        candidates.append(_to_accommodation_result(acc, wk, lv, lc, coord_map))

    sessions.pop(body.thread_id, None)

    region_name = final.recommended_region or selected.get("region_name", "")
    return RecommendResponse(
        recommended_region=region_name,
        results_subtitle=f"{region_name}에서 선별된 숙소 {len(candidates)}곳을 종합 점수 순으로 정렬했어요",
        matched_conditions=final.matched_conditions or [],
        candidates=candidates,
    )
