"""
Living Agent — 생활 인프라 평가 (ReAct 에이전트).

실행 흐름:
  1. Quick Scan   : 첫 번째 숙소 좌표로 지역 인프라 현황 파악 (Kakao, 빠름)
  2. 숙소별 병렬  : ReAct 루프 — LLM이 search_category 도구를 자율 호출
                   → 결과 집계 → Evaluation LLM
  3. 재평가       : 검색 결과 0건 OR confidence < 54인 숙소에 한해 1회 재실행
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Literal, Optional, Tuple

from langsmith import traceable

from app.config.settings import (
    RETRY_CONFIDENCE_THRESHOLD,
    RETRY_MAX_COUNT,
    RETRY_RESULT_EMPTY,
    LLM_MODEL_SONNET,
)
from app.core.llm import call_llm, call_llm_with_tools
from app.core.state import GraphState
from app.prompts.living_prompts import EVALUATION_SYSTEM
from app.schemas.living_schema import (
    CategoryResult,
    LivingAssessment,
    LivingDetails,
)
from app.schemas.worker import LivingEvaluation
from app.tools.geo import haversine_meters as _haversine
from app.tools.living_tool import (
    geocode_address,
    quick_scan,
    reverse_geocode,
    search_bus_stops_near,
    search_medical_near,
    search_one_category,
)

logger = logging.getLogger(__name__)

_SCAN_RADIUS: Dict[str, float] = {"walk": 3.0, "car": 10.0}
_MODEL_EVALUATION = LLM_MODEL_SONNET
_DEFAULT_WEIGHTS = {"transport": 0.25, "grocery": 0.25, "medical": 0.25, "services": 0.25}
_CATS = ("transport", "grocery", "medical", "services")


# ── ReAct 도구 정의 ────────────────────────────────────────────────────────────

_SEARCH_CATEGORY_TOOL: dict = {
    "name": "search_category",
    "description": (
        "숙소 주변 생활 인프라 카테고리 하나를 탐색합니다. "
        "결과가 0건이면 다른 keywords나 더 넓은 radius_m으로 재호출하세요."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["transport", "grocery", "medical", "services"],
                "description": "탐색할 카테고리",
            },
            "kakao_codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Kakao Local 카테고리 코드 목록. "
                    "SW8=지하철역, BS8=버스터미널, MT1=대형마트, CS2=편의점, "
                    "HP8=병원, PM9=약국, BK9=은행, PO3=우체국"
                ),
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Kakao 키워드 검색어 목록 (예: ['버스정류장', '시내버스', '기차역'])",
            },
            "naver_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Naver 키워드 검색어 목록. Kakao에 없는 장소 보완용 (예: ['탐정형외과', '한림의원'])",
            },
            "radius_m": {
                "type": "integer",
                "description": (
                    "탐색 반경(미터). "
                    "도보 기본 1500 / 재검색 2500. "
                    "자차 기본 10000 / 재검색 15000."
                ),
            },
        },
        "required": ["category", "radius_m"],
    },
}

REACT_SYSTEM = """
모든 응답은 반드시 한국어로 작성하세요. Do not respond in English under any circumstances.

당신은 워케이션 숙소의 생활 인프라를 평가하는 AI 에이전트입니다.
search_category 도구를 자율 호출해 4개 카테고리를 탐색하세요.

[카테고리]
- transport : 대중교통 (지하철역, 버스터미널, 기차역, KTX역, 시내버스 등)
- grocery   : 식료품 (대형마트, 편의점, 전통시장, 슈퍼마켓 등)
- medical   : 의료 (병원, 약국, 보건소 등)
- services  : 생활서비스 (은행, ATM, 우체국, 세탁소, 헬스장)

[Kakao 카테고리 코드]
SW8=지하철역  BS8=버스터미널(시외·고속버스 전용)  MT1=대형마트  CS2=편의점
HP8=병원      PM9=약국                             BK9=은행      PO3=우체국

[services 검색 기준]
기본 키워드 (항상 포함): kakao_codes=["BK9","PO3"], keywords=["ATM","세탁소","코인세탁","헬스장"]
사용자가 명시적으로 언급한 경우에만 keywords에 추가:
  예) "반려동물 동반" → "동물병원" 추가 (펫숍은 포함하지 않음)
  예) "아이와 함께" → "놀이터" 추가
  예) "수영하고 싶다" → "수영장" 추가, 해안·섬 지역이면 "해수욕장" 추가 (내륙이면 제외)
노인복지관·경로당·청소년센터 등 특정 연령·대상 시설은
사용자가 명시하지 않으면 절대 검색하지 마세요.

주의: BS8은 시외·고속버스 터미널만 잡힙니다.
일반 시내버스 정류장은 Kakao에 POI로 등록되지 않으므로
keywords: ["버스정류장", "시내버스"] + naver_keywords: ["버스정류장", "마을버스"] 를 함께 사용하세요.

[Kakao vs Naver 역할 분담]
Kakao keywords : 위치 기반 반경 검색. 대형 시설(마트, 터미널, 대형병원)에 강함.
Naver keywords : 텍스트 검색. Kakao에 없는 소형 의원·버스정류장·지역 시설에 강함.

다음 경우 반드시 naver_keywords를 함께 사용하세요:
- transport: 버스정류장, 마을버스, 시내버스 (Kakao에 POI 없음)
- medical  : Kakao HP8은 대형병원 위주. 소형 의원은 진료과명이 장소명에 포함됨.
  keywords(Kakao 위치기반): ["정형외과", "내과", "치과", "한의원", "피부과", "이비인후과"]
  naver_keywords: 위와 동일하게 설정 (크로스 검증용)
  Kakao keyword 검색은 좌표+반경 기반이므로 진료과명을 keywords에 반드시 포함하세요.
- 0건이 나온 뒤 재검색 시: naver_keywords를 추가하거나 교체하세요.

[초기 반경 설정]
사용자가 거리 조건을 명시한 경우:
  "도보 N분" → radius_m = N × 80 (80m/분 기준)
  "Nkm 이내" → radius_m = N × 1000
  재검색 시: radius_m × 2

사용자 조건 미명시 — quick_scan 결과 기준:
  found=True 카테고리가 많은 도심 → 도보 1500m / 자차 10000m
  found=False 카테고리가 많은 농촌·도서 지역 → 도보 2500m / 자차 15000m
  재검색 시: radius_m × 2 (도보 최대 5000m)

[도구 호출 규칙 — 반드시 준수]
★ 첫 번째 응답에서 transport, grocery, medical, services 4개를 동시에 호출하세요.
  하나씩 순서대로 호출하면 안 됩니다. 반드시 4개를 한 번에 호출하세요.
★ 결과를 받은 후 0건인 카테고리만 재검색하세요. 재검색도 여러 개면 동시에 호출하세요.
★ 모든 카테고리 결과가 나오면 도구 호출을 멈추세요.
""".strip()


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def _transport_mode(parsed_preferences: Dict[str, Any]) -> Literal["walk", "car"]:
    val = str(parsed_preferences.get("transport") or "").lower()
    if any(k in val for k in ("자차", "자동차", "car", "drive")):
        return "car"
    return "walk"


def _acc_id(acc: Dict[str, Any]) -> str:
    return str(acc.get("id") or acc.get("accommodation_id") or acc.get("name") or "unknown")


async def _coordinates(acc: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    lat = acc.get("latitude") or acc.get("lat")
    lng = acc.get("longitude") or acc.get("lng") or acc.get("lon")
    if lat and lng:
        return float(lat), float(lng)
    address = acc.get("address") or acc.get("addr")
    if address:
        return await geocode_address(str(address))
    return None


# ── Evaluation LLM ────────────────────────────────────────────────────────────

async def _evaluate(
    acc_id: str,
    details: LivingDetails,
    parsed_preferences: Dict[str, Any],
) -> Optional[LivingAssessment]:
    """수집된 LivingDetails → score / confidence / summary."""
    content = (
        f"숙소 ID: {acc_id}\n\n"
        f"생활 인프라 탐색 결과:\n"
        f"{json.dumps(details.model_dump(), ensure_ascii=False, indent=2)}\n\n"
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


# ── 숙소 단위 ReAct 처리 ──────────────────────────────────────────────────────

@traceable(name="living_process_react")
async def _process_react(
    acc: Dict[str, Any],
    scan_data: Dict[str, Any],
    transport_mode: str,
    parsed_preferences: Dict[str, Any],
    retry_hint: Optional[str] = None,
) -> LivingEvaluation:
    """숙소 하나: ReAct 루프 (LLM이 search_category 자율 호출) → Evaluation."""
    aid = _acc_id(acc)

    coords = await _coordinates(acc)
    if not coords:
        return LivingEvaluation(
            accommodation_id=aid,
            summary="좌표를 확인할 수 없어 생활 인프라 평가를 수행하지 못했습니다.",
        )

    lat, lng = coords
    area_name = await reverse_geocode(lat, lng)

    system = REACT_SYSTEM
    if retry_hint:
        system += f"\n\n재탐색 안내: {retry_hint}"

    messages = [
        {
            "role": "user",
            "content": (
                f"숙소 ID: {aid}\n"
                f"주소: {acc.get('address', '미입력')}\n"
                f"좌표: 위도 {lat:.6f}, 경도 {lng:.6f}\n\n"
                f"사용자 조건:\n{json.dumps(parsed_preferences, ensure_ascii=False, indent=2)}\n\n"
                f"지역 사전 탐색 결과 (참고용):\n{json.dumps(scan_data, ensure_ascii=False, indent=2)}\n\n"
                f"이동 방식: {transport_mode}\n\n"
                f"4개 카테고리(transport, grocery, medical, services)를 탐색하세요."
            ),
        }
    ]

    async def tool_executor(name: str, args: dict):
        if name != "search_category":
            raise ValueError(f"알 수 없는 도구: {name}")
        category       = args.get("category", "")
        codes          = args.get("kakao_codes", [])
        keywords       = args.get("keywords", [])
        naver_keywords = args.get("naver_keywords", [])
        radius_m       = int(args.get("radius_m", 1500))

        logger.info(
            "[Living][%s] search_category cat=%s codes=%s kw=%s naver=%s r=%dm",
            aid, category, codes, keywords, naver_keywords, radius_m,
        )

        result: CategoryResult = await search_one_category(
            lat, lng,
            kakao_codes=codes,
            keywords=keywords,
            radius_m=radius_m,
            transport_mode=transport_mode,
            area_name=area_name,
            naver_keywords=naver_keywords,
        )

        # medical 카테고리 → HIRA 의료기관 XLSX로 보강
        if category == "medical":
            medical_stops = search_medical_near(lat, lng, radius_m, sido=area_name.split()[0] if area_name else "")
            if medical_stops:
                existing = {p.name for p in (result.places or [])}
                new_medical = [p for p in medical_stops if p.name not in existing]
                merged_places = sorted(
                    list(result.places or []) + new_medical,
                    key=lambda p: p.distance_meters,
                )[:15]
                result = CategoryResult(
                    found=True,
                    zone_km=radius_m / 1000,
                    count=len(merged_places),
                    nearest_minutes=merged_places[0].distance_meters // 80 if merged_places else None,
                    places=merged_places,
                    source=result.source if result.found else "kakao_only",
                )
                logger.info("[Living][%s] medical + HIRA → count=%d", aid, result.count)

        # transport 카테고리 → 버스정류장 CSV로 보강
        if category == "transport":
            bus_stops = search_bus_stops_near(lat, lng, radius_m, city_name=area_name)
            if bus_stops:
                existing = set(p.name for p in (result.places or []))
                new_stops = [p for p in bus_stops if p.name not in existing]
                merged_places = sorted(
                    list(result.places or []) + new_stops,
                    key=lambda p: p.distance_meters,
                )[:10]
                result = CategoryResult(
                    found=True,
                    zone_km=radius_m / 1000,
                    count=len(merged_places),
                    nearest_minutes=merged_places[0].distance_meters // 80 if merged_places else None,
                    places=merged_places,
                    source=result.source if result.found else "kakao_only",
                )
                logger.info("[Living][%s] transport + bus_stops → count=%d", aid, result.count)

        logger.info(
            "[Living][%s] %s → found=%s count=%d",
            aid, category, result.found, result.count,
        )
        summary = f"{category}: {'발견' if result.found else '미발견'} {result.count}건"
        return summary, {"category": category, "result": result}

    # LLM이 search_category를 자율 호출 (최대 8라운드: 카테고리 4 × 재검색 포함)
    payloads = await call_llm_with_tools(
        messages=messages,
        tools=[_SEARCH_CATEGORY_TOOL],
        tool_executor=tool_executor,
        system=system,
        max_rounds=8,
        max_tokens=1024,
    )

    # 카테고리별 최적 결과 집계 (같은 카테고리 여러 번 호출 시 count 많은 것 유지)
    cat_results: Dict[str, CategoryResult] = {}
    for p in payloads:
        cat    = p["category"]
        result = p["result"]
        prev   = cat_results.get(cat)
        if prev is None or result.count > prev.count:
            cat_results[cat] = result

    default_cat = CategoryResult(found=False, source="none")
    details = LivingDetails(
        transport=cat_results.get("transport", default_cat),
        grocery  =cat_results.get("grocery",   default_cat),
        medical  =cat_results.get("medical",   default_cat),
        services =cat_results.get("services",  default_cat),
        weights_applied=_DEFAULT_WEIGHTS,
    )

    assessment = await _evaluate(aid, details, parsed_preferences)
    if not assessment:
        return LivingEvaluation(accommodation_id=aid, details=details.model_dump())

    return LivingEvaluation(
        accommodation_id=aid,
        score=assessment.score,
        confidence=assessment.confidence,
        summary=assessment.summary,
        details=details.model_dump(),
    )


# ── 메인 에이전트 ─────────────────────────────────────────────────────────────

@traceable(name="living_agent")
async def living_agent(state: GraphState) -> GraphState:
    """
    생활 인프라 평가 에이전트 (ReAct).

    읽는 state : candidate_accommodations, parsed_preferences, must_have_conditions, retry_count
    쓰는 state : living_evaluations, retry_count, errors
    """
    parsed_preferences: Dict[str, Any] = state.get("parsed_preferences") or {}
    must_have_conditions: List[str]    = state.get("must_have_conditions") or []
    candidates: List[Dict[str, Any]]   = state.get("candidate_accommodations") or []
    retry_count: Dict[str, int]        = dict(state.get("retry_count") or {})
    errors: List[str]                  = list(state.get("errors") or [])

    if must_have_conditions:
        parsed_preferences = {**parsed_preferences, "must_have_conditions": must_have_conditions}

    if not candidates:
        return {**state, "living_evaluations": [], "errors": errors}

    transport_mode = _transport_mode(parsed_preferences)

    # ── 1. Quick Scan — 숙소별 좌표 기반, 근접 지역은 결과 재사용 ─────────────
    _SCAN_REUSE_M   = 500                                          # 500m 미만이면 같은 동네로 판단
    default_scan    = {"scan_radius_km": _SCAN_RADIUS[transport_mode]}

    # 모든 숙소 좌표 병렬 조회
    raw_coords = await asyncio.gather(
        *[_coordinates(acc) for acc in candidates], return_exceptions=True
    )
    coords_list = [c if not isinstance(c, Exception) else None for c in raw_coords]

    # 고유 스캔 지점 선별 (500m 미만이면 같은 동네로 판단해 scan 재사용)
    unique_coords: List[tuple] = []
    for coords in coords_list:
        if coords is None:
            continue
        if not any(
            _haversine(coords[0], coords[1], uc[0], uc[1]) < _SCAN_REUSE_M
            for uc in unique_coords
        ):
            unique_coords.append(coords)

    # 고유 지점만 병렬 스캔
    if unique_coords:
        scan_results = await asyncio.gather(
            *[quick_scan(*c, _SCAN_RADIUS[transport_mode]) for c in unique_coords]
        )
        scan_map = dict(zip(unique_coords, scan_results))
    else:
        scan_map = {}

    def _pick_scan(coords: Optional[tuple]) -> Dict:
        """숙소 좌표와 가장 가까운 scan_data 반환."""
        if not coords or not scan_map:
            return default_scan
        nearest = min(scan_map, key=lambda uc: _haversine(coords[0], coords[1], uc[0], uc[1]))
        return scan_map[nearest]

    scan_data_list = [_pick_scan(c) for c in coords_list]

    # ── 2. 숙소별 병렬 ReAct ──────────────────────────────────────────────────
    evaluations: List[LivingEvaluation] = list(
        await asyncio.gather(
            *[_process_react(acc, scan_data, transport_mode, parsed_preferences)
              for acc, scan_data in zip(candidates, scan_data_list)]
        )
    )

    # ── 3. 재평가 대상 선별 후 재호출 (1회 한정) ─────────────────────────────
    # 조건: 검색 결과 0건 OR confidence < 54 (좌표 없음 제외)
    if retry_count.get("living", 0) < RETRY_MAX_COUNT:
        low = [
            e for e in evaluations
            if e.details
            and (
                (RETRY_RESULT_EMPTY and all(
                    e.details.get(c, {}).get("count", 0) == 0 for c in _CATS
                ))
                or (e.confidence is not None and e.confidence < RETRY_CONFIDENCE_THRESHOLD)
            )
        ]
        if low:
            low_ids = {e.accommodation_id for e in low}

            def _build_retry_hint(e: LivingEvaluation) -> str:
                zero_cats = [
                    c for c in _CATS
                    if e.details.get(c, {}).get("count", 0) == 0
                ]
                good_cats = [c for c in _CATS if c not in zero_cats]
                parts = []
                if zero_cats:
                    parts.append(f"{zero_cats} 카테고리가 0건이었습니다. 다른 키워드와 넓은 반경으로 재검색하세요.")
                if good_cats:
                    parts.append(f"{good_cats} 카테고리는 결과가 충분하므로 건너뛰세요.")
                if e.confidence is not None and e.confidence < RETRY_CONFIDENCE_THRESHOLD:
                    parts.append(f"신뢰도가 {e.confidence}점으로 낮습니다. 더 다양한 키워드를 사용하세요.")
                return " ".join(parts)

            eval_map = {e.accommodation_id: e for e in low}
            retry_pairs = [
                (acc, scan_data_list[i], _build_retry_hint(eval_map[_acc_id(acc)]))
                for i, acc in enumerate(candidates)
                if _acc_id(acc) in low_ids
            ]
            retry_evals: List[LivingEvaluation] = list(
                await asyncio.gather(
                    *[_process_react(acc, sd, transport_mode, parsed_preferences, retry_hint=hint)
                      for acc, sd, hint in retry_pairs]
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
