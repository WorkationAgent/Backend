import asyncio
import uuid
from typing import Any
from fastapi import APIRouter, HTTPException
from langgraph.types import Command

from app.graph.workflow import graph
from app.tools import kto
from app.api.schemas import (
    PlanRequest, PlanResponse, ParsedConditions, RegionCandidate,
    SelectRegionRequest, RecommendResponse, AccommodationResult,
    CategoryScores, EvaluatedItem, EvaluationSection, MapPoint,
    AccommodationInfo, LivingCategoryItem,
)

router = APIRouter()

# 세션 상태는 LangGraph 체크포인터(MemorySaver)가 thread_id로 관리한다.
# (인메모리 — 서버 재시작 시 소실. 운영 전환 시 SQLite/Postgres saver로 교체)


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


_LIVING_LABELS = {
    "transport": "교통",
    "grocery":   "식료품",
    "medical":   "의료",
    "services":  "서비스",
}


def _local_radius_m(local_eval) -> float | None:
    det = getattr(local_eval, "details", None)
    km = getattr(det, "search_radius_used_km", 0) or 0
    return round(km * 1000) if km else None


def _living_radius_m(living_eval) -> float | None:
    d = getattr(living_eval, "details", None)
    if not isinstance(d, dict):
        return None
    radii = []
    for cat in ("transport", "grocery", "medical", "services"):
        c = d.get(cat) or {}
        if isinstance(c, dict) and c.get("zone_km"):
            radii.append(float(c["zone_km"]) * 1000)
    return round(max(radii)) if radii else None


def _work_radius_m(work_eval) -> float | None:
    d = getattr(work_eval, "details", None)
    if isinstance(d, dict) and d.get("search_radius_km"):
        return round(float(d["search_radius_km"]) * 1000)
    return None


def _work_map_points(work_eval) -> list[MapPoint]:
    """work_eval.map_points([{name,lat,lng,type}]) → 프론트 MapPoint(kind=work).
    LLM을 거치지 않고 work 에이전트의 실제 좌표를 직접 사용."""
    pts: list[MapPoint] = []
    for mp in (getattr(work_eval, "map_points", None) or []):
        if not isinstance(mp, dict):
            continue
        lat, lng = mp.get("lat"), mp.get("lng")
        if lat is None or lng is None:
            continue
        try:
            pts.append(MapPoint(
                name=mp.get("name", "") or "",
                kind="work",
                lat=float(lat),
                lng=float(lng),
            ))
        except Exception:
            pass
    return pts


def _living_map_points(living_eval) -> list[MapPoint]:
    """living_eval.details의 카테고리별 가장 가까운 장소 1곳을 kind=living 핀으로."""
    d = getattr(living_eval, "details", None)
    if not isinstance(d, dict):
        return []
    pts: list[MapPoint] = []
    for cat in ("transport", "grocery", "medical", "services"):
        c = d.get(cat) or {}
        if not isinstance(c, dict):
            continue
        places = [
            p for p in (c.get("places") or [])
            if isinstance(p, dict) and p.get("latitude") is not None and p.get("longitude") is not None
        ]
        if not places:
            continue
        p = min(places, key=lambda x: x.get("distance_meters") or 9e9)
        try:
            pts.append(MapPoint(name=p.get("name", "") or "", kind="living",
                                lat=float(p["latitude"]), lng=float(p["longitude"])))
        except Exception:
            pass
    return pts


def _local_map_points(local_eval) -> list[MapPoint]:
    """local_eval.details의 signature/daily spots(좌표 채워진) → kind=local 핀."""
    det = getattr(local_eval, "details", None)
    if det is None:
        return []
    spots = list(getattr(det, "signature_spots", []) or []) + list(getattr(det, "daily_spots", []) or [])
    pts: list[MapPoint] = []
    seen: set[str] = set()
    for s in spots:
        lat = getattr(s, "latitude", None)
        lng = getattr(s, "longitude", None)
        name = getattr(s, "name", "") or ""
        if lat is None or lng is None or not name or name in seen:
            continue
        seen.add(name)
        try:
            pts.append(MapPoint(name=name, kind="local", lat=float(lat), lng=float(lng)))
        except Exception:
            pass
    return pts


def _living_categories(living_eval) -> list[LivingCategoryItem]:
    """living_eval.details(LivingDetails) → 카테고리별 대표 장소 1곳.

    각 카테고리(transport/grocery/medical/services)에서 가장 가까운 place 1개를
    뽑아 거리 문구(도보 N분)와 함께 반환. 결과 없으면 found=False.
    """
    d = getattr(living_eval, "details", None)
    if not isinstance(d, dict):
        return []
    out: list[LivingCategoryItem] = []
    for key, label in _LIVING_LABELS.items():
        c = d.get(key) or {}
        if not isinstance(c, dict):
            out.append(LivingCategoryItem(category=key, label=label, found=False))
            continue
        places = [p for p in (c.get("places") or []) if isinstance(p, dict)]
        if not places:
            out.append(LivingCategoryItem(category=key, label=label, found=bool(c.get("found"))))
            continue
        nearest = min(places, key=lambda x: x.get("distance_meters") or 9e9)
        dm = nearest.get("distance_meters")
        nm = c.get("nearest_minutes")
        if nm:
            dist = f"도보 {nm}분"
        elif dm:
            dist = f"도보 약 {round(dm / 80)}분"
        else:
            dist = ""
        out.append(LivingCategoryItem(
            category=key, label=label,
            name=nearest.get("name", "") or "",
            distance_text=dist, found=True,
        ))
    return out


def _to_evaluated_items(items: list) -> list[EvaluatedItem]:
    result = []
    for item in (items or []):
        try:
            d = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        except Exception:
            d = {}
        result.append(EvaluatedItem(
            name=d.get("name") or "",
            sub=(d.get("description") or d.get("sub") or ""),
            distance_text=d.get("distance_text") or None,
        ))
    return result


def _to_accommodation_result(ranked: Any, work_eval, living_eval, local_eval,
                              coord_map: dict | None = None,
                              skipped_agents: dict | None = None) -> AccommodationResult:
    # 좌표 — normalized_accommodations에서 acc_id로 조회
    acc_id = str(ranked.accommodation_id)
    coords = (coord_map or {}).get(acc_id, {})
    lat = float(coords.get("latitude") or 0.0)
    lng = float(coords.get("longitude") or 0.0)

    # MapPoints — 세 에이전트 eval의 실제 좌표로 직접 생성 (LLM 핀 미사용, 통일).
    # stay 핀은 프론트가 center로 합성하므로 백엔드에선 work/living/local만 제공.
    map_points = (
        _work_map_points(work_eval)
        + _living_map_points(living_eval)
        + _local_map_points(local_eval)
    )

    # sections — 스킵된 에이전트도 빈 섹션으로 항상 포함 (프론트 undefined 방지)
    # 미실행 에이전트는 skip_reason 문구를 담아 프론트에 안내
    _sk = skipped_agents or {}

    def _empty(kind: str) -> EvaluationSection:
        reason = _sk.get(kind, "")
        return EvaluationSection(skipped=bool(reason), skip_reason=reason)

    sections: dict[str, EvaluationSection] = {
        "work":   _empty("work"),
        "living": _empty("living"),
        "local":  _empty("local"),
    }

    if work_eval:
        work_items = _to_evaluated_items(ranked.work_environment)
        # work 장소의 이동시간(distance_min, 분)을 이름 매칭으로 distance_text에 채움
        wd = getattr(work_eval, "details", None)
        if isinstance(wd, dict):
            dist_map = {
                p.get("name"): p.get("distance_min")
                for p in (wd.get("places") or [])
                if isinstance(p, dict) and p.get("name") and p.get("distance_min")
            }
            for it in work_items:
                if not it.distance_text and dist_map.get(it.name):
                    it.distance_text = f"약 {dist_map[it.name]}분"
        sections["work"] = EvaluationSection(
            score=round(work_eval.score or 0),
            summary=ranked.work_summary or "",
            items=work_items,
            search_radius_m=_work_radius_m(work_eval),
        )

    if living_eval:
        sections["living"] = EvaluationSection(
            score=round(living_eval.score or 0),
            summary=ranked.living_summary or "",
            items=_to_evaluated_items(ranked.living_elements),
            search_radius_m=_living_radius_m(living_eval),
        )

    if local_eval:
        sections["local"] = EvaluationSection(
            score=round(local_eval.score or 0),
            summary=ranked.local_summary or "",
            items=_to_evaluated_items(ranked.local_experiences),
            search_radius_m=_local_radius_m(local_eval),
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
        living_categories=_living_categories(living_eval),
        accommodation_info=acc_info,
    )


@router.post("/plan", response_model=PlanResponse)
async def plan(body: PlanRequest):
    """줄글 입력 → 생활권 후보 3개 반환.

    그래프를 시작해 human_select 노드의 interrupt에서 멈춘다.
    멈춘 시점의 state(candidate_regions·해석 조건)를 읽어 응답한다.
    """
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    await graph.ainvoke(
        {"raw_user_input": body.text, "errors": [], "warnings": [], "retry_count": {}},
        config=config,
    )
    snapshot = await graph.aget_state(config)
    values = snapshot.values
    regions = values.get("candidate_regions", [])

    candidates = [
        _to_region_candidate(r, i)
        for i, r in enumerate(regions)
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
            must_have=values.get("must_have_conditions", []),
            preferences=values.get("preference_conditions", []),
        ),
        candidate_regions=candidates,
    )


@router.post("/select-region", response_model=RecommendResponse)
async def select_region(body: SelectRegionRequest):
    """지역 선택 → 그래프 재개(숙소 탐색·평가·통합) → 최종 추천 반환."""
    config = {"configurable": {"thread_id": body.thread_id}}
    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="세션이 없거나 만료됐어요. 처음부터 다시 시작해주세요.")

    candidate_regions = snapshot.values.get("candidate_regions", [])
    selected = next(
        (r for r in candidate_regions if r.get("region_id") == body.region_id),
        candidate_regions[0] if candidate_regions else None,
    )
    if not selected:
        raise HTTPException(status_code=400, detail="선택한 지역을 찾을 수 없어요.")

    # human_select의 interrupt에 선택 지역을 주입하고 그래프를 끝까지 재개
    result = await graph.ainvoke(Command(resume=selected), config=config)

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
        candidates.append(
            _to_accommodation_result(acc, wk, lv, lc, coord_map, result.get("skipped_agents"))
        )

    region_name = final.recommended_region or selected.get("region_name", "")
    return RecommendResponse(
        recommended_region=region_name,
        results_subtitle=f"{region_name}에서 선별된 숙소 {len(candidates)}곳을 종합 점수 순으로 정렬했어요",
        matched_conditions=final.matched_conditions or [],
        candidates=candidates,
    )
