from __future__ import annotations
import asyncio
import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.config.settings import (
    SEARCH_RADIUS_LOCAL_M,
    SEARCH_RADIUS_CAR_M,
    RETRY_CONFIDENCE_THRESHOLD,
    RETRY_RADIUS_EXPAND_M,
    RETRY_CAR_EXPAND_M,
    RETRY_MAX_COUNT,
)
from app.core.llm import call_llm, call_llm_with_tools
from app.prompts.local_agent import SYSTEM_PROMPT, build_user_prompt
from app.schemas.user_input import UserInput
from app.schemas.worker import LocalEvaluation
from app.tools import kto, kakao, naver
from app.tools.geo import haversine_meters
from app.tools.rag import retrieve_regional_context


# ─────────────────────────────────────────────────────────────────────────────
# Entry point — state 기반 (Planner 호출용)
# ─────────────────────────────────────────────────────────────────────────────
async def local_agent(state: dict) -> dict:
    """Planner가 호출하는 통일된 진입점.

    읽기: state["candidate_accommodations"], state["user_input"],
          state["must_have_conditions"]
    쓰기: {"local_evaluations": [...], "warnings": [...]}
    """
    accommodations: list[dict] = state.get("candidate_accommodations", [])
    user_input: UserInput = state["user_input"]
    stay_dates = state.get("parsed_preferences", {}).get("stay_dates")
    must_have_conditions: list[str] = state.get("must_have_conditions", [])

    # must_have를 user_input에 additional_request로 주입 (Local Agent가 활용)
    if must_have_conditions and not user_input.additional_request:
        from dataclasses import replace
        try:
            user_input = user_input.model_copy(
                update={"additional_request": " | ".join(must_have_conditions)}
            )
        except Exception:
            pass

    evaluations = await evaluate_accommodations(accommodations, user_input, stay_dates)
    return {"local_evaluations": list(evaluations), "warnings": []}


async def evaluate_accommodations(
    accommodations: list[dict],
    user_input: UserInput,
    stay_dates: Optional[tuple[str, str]] = None,
) -> list[LocalEvaluation]:
    """숙소 목록 병렬 평가 (직접 호출용)."""
    tasks = [
        _evaluate_one(acc, user_input, stay_dates)
        for acc in accommodations
    ]
    return await asyncio.gather(*tasks)


# ─────────────────────────────────────────────────────────────────────────────
# 숙소 1개 평가 — 하이브리드 3단계
# ─────────────────────────────────────────────────────────────────────────────
async def _evaluate_one(
    accommodation: dict,
    user_input: UserInput,
    stay_dates: Optional[tuple[str, str]],
    retry: int = 0,
) -> LocalEvaluation:
    radius_m = _initial_radius_m(user_input, retry)

    # 1) 병렬 베이스라인 수집 (LLM 없음)
    signals = await _collect_signals(accommodation, user_input, stay_dates, radius_m)
    signature_places = signals["signature_places"]
    signature_source = signals["signature_source"]
    daily_places     = signals["daily_places"]
    festivals        = signals["festivals"]
    blog_snippets    = signals["blog_snippets"]
    regional_context = signals["regional_context"]

    # 결과 0건 → 반경 확장 후 즉시 재호출 (LLM 비용 절약)
    total = (
        len(signature_places)
        + sum(len(v) for v in daily_places.values())
        + len(festivals)
    )
    if total == 0 and retry < RETRY_MAX_COUNT:
        return await _evaluate_one(accommodation, user_input, stay_dates, retry + 1)

    # 1.5) 자율 보강 — hobby/vibe 특화 장소를 LLM이 0~3번 자율 검색 (미니 ReAct)
    augmented_places = await _autonomous_augment(accommodation, user_input, radius_m)

    # 2) LLM 평가 (베이스라인 + 보강 결과 전달)
    user_msg = build_user_prompt(
        accommodation=accommodation,
        user_input=user_input.model_dump(),
        signature_places=[p.model_dump() for p in signature_places],
        signature_source=signature_source,
        daily_places={k: [p.model_dump() for p in v] for k, v in daily_places.items()},
        augmented_places=[p.model_dump() for p in augmented_places],
        festivals=[f.model_dump() for f in festivals],
        blog_snippets=[b.model_dump() for b in blog_snippets],
        regional_context=[c.model_dump() for c in regional_context],
        search_radius_used_km=radius_m / 1000,
    )

    evaluation: LocalEvaluation = await call_llm(
        messages=[{"role": "user", "content": user_msg}],
        system=SYSTEM_PROMPT,
        output_schema=LocalEvaluation,
        max_tokens=2048,
    )
    evaluation.accommodation_id = accommodation["id"]

    # 3) confidence 부족 → 반경 확장 재호출
    if (
        evaluation.confidence <= RETRY_CONFIDENCE_THRESHOLD
        and retry < RETRY_MAX_COUNT
    ):
        return await _evaluate_one(accommodation, user_input, stay_dates, retry + 1)

    return evaluation


# ─────────────────────────────────────────────────────────────────────────────
# 자율 보강 — hobby/vibe 특화 검색 (하이브리드 2단계, 미니 ReAct 루프)
# ─────────────────────────────────────────────────────────────────────────────
async def _autonomous_augment(
    accommodation: dict,
    user_input: UserInput,
    radius_m: int,
    max_searches: int = 3,
) -> list:
    """사용자 취향 특화 장소를 LLM이 자율적으로 0~max_searches번 검색.

    일반 카테고리(카페/음식점/관광지)로 잡히지 않는 것(예: 도예 공방, 비건 식당).
    hobby/vibe 신호가 없으면 검색 없이 빈 리스트 반환(베이스라인만으로 평가).
    """
    hints = [user_input.tourism_hobby, user_input.desired_vibe]
    if not any(h and h.strip() for h in hints):
        return []

    lat = accommodation["latitude"]
    lng = accommodation["longitude"]
    region = accommodation.get("region", "")

    tools = [{
        "name": "search_places_by_keyword",
        "description": (
            "사용자 취향에 특화된 장소를 키워드로 검색. 일반 카테고리(카페/음식점/"
            "관광지)로 잡히는 것에만 사용. 예: '도예 공방', '비건 식당', '낚시터'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 키워드 (지역명 제외, 취향만)"},
            },
            "required": ["query"],
        },
    }]

    async def _executor(name: str, inp: dict):
        if name == "search_places_by_keyword":
            q = f"{region} {inp.get('query', '')}".strip()
            try:
                places = await kakao.keyword_search(q, lat, lng, radius_m, size=5)
            except Exception:
                return "검색 실패", []
            for p in places:
                if p.distance_meters is None:
                    p.distance_meters = haversine_meters(lat, lng, p.latitude, p.longitude)
            summary = f"{len(places)}건: " + ", ".join(p.name for p in places[:5])
            return (summary or "0건"), places
        return "알 수 없는 도구", []

    system = (
        "사용자 취향에 특화된 장소를 찾는 보조 도구입니다. "
        "일반 카테고리로 잡히는 것만 검색하고, 충분하면 즉시 멈추세요. 불필요하면 검색하지 마세요."
    )
    prompt = (
        f"사용자 취향: hobby={user_input.tourism_hobby}, vibe={user_input.desired_vibe}\n"
        f"지역: {region}\n"
        f"이 취향에 특화된 장소를 최대 {max_searches}번까지 검색하세요. "
        f"일반 관광지/카페로 충분히 커버되면 검색하지 마세요."
    )

    collected = await call_llm_with_tools(
        messages=[{"role": "user", "content": prompt}],
        tools=tools,
        tool_executor=_executor,
        system=system,
        max_rounds=max_searches,
    )

    # flatten + place_id 기준 중복 제거
    seen, flat = set(), []
    for batch in collected:
        for p in batch:
            if p.place_id not in seen:
                seen.add(p.place_id)
                flat.append(p)
    return flat


# ─────────────────────────────────────────────────────────────────────────────
# 신호 수집 — 시그니처(지역 대표) + 일상(proximity) 두 갈래
# ─────────────────────────────────────────────────────────────────────────────
async def _collect_signals(
    accommodation: dict,
    user_input: UserInput,
    stay_dates: Optional[tuple[str, str]],
    radius_m: int,
) -> dict:
    lat = accommodation["latitude"]
    lng = accommodation["longitude"]
    region = accommodation.get("region", "")

    # (A) 지역 시그니처 — areaBasedList2 (행정 구역 대표 명소) + RAG
    signature_task = _fetch_signature_places(accommodation)
    rag_task = retrieve_regional_context(
        region=region,
        user_hints=[
            user_input.tourism_hobby or "",
            user_input.desired_vibe or "",
            user_input.region_style or "",
        ],
        top_k=5,
    )

    # (B) 매일 들를 거리 — proximity (Kakao 카페/맛집 + KTO 주변)
    daily_kto = [
        kto.location_based_list(lat, lng, radius_m, kto.CONTENT_TYPE["tourist_spot"]),
        kto.location_based_list(lat, lng, radius_m, kto.CONTENT_TYPE["leports"]),
    ]
    daily_kakao = [
        kakao.category_search(lat, lng, radius_m, "CE7"),  # 분위기 카페
        kakao.category_search(lat, lng, radius_m, "FD6"),  # 로컬 맛집
    ]

    # 축제 (시그니처 보너스용)
    festival_task = (
        kto.search_festival(lat, lng, radius_m, stay_dates[0], stay_dates[1])
        if stay_dates else _empty()
    )

    # 블로그 후기 (hobby/vibe 있을 때만)
    blog_task = _maybe_blog_search(user_input, accommodation)

    (signature_result, regional_context,
     kto_tourist, kto_leports,
     kakao_cafe, kakao_food,
     festivals, blog_snippets) = await asyncio.gather(
        signature_task, rag_task,
        *daily_kto, *daily_kakao,
        festival_task, blog_task,
    )
    signature_places, signature_source = signature_result

    daily_places = {
        "kakao_vibe_cafe":   kakao_cafe,
        "kakao_local_food":  kakao_food,
        "kto_nearby_spots":  kto_tourist,
        "kto_leports":       kto_leports,
    }
    return {
        "signature_places":  signature_places,
        "signature_source":  signature_source,
        "daily_places":      daily_places,
        "festivals":         festivals,
        "blog_snippets":     blog_snippets,
        "regional_context":  regional_context,
    }


async def _fetch_signature_places(accommodation: dict):
    """지역 대표 명소 + 각 명소의 숙소 거리.

    좌표에서 KTO 코드를 도출(_resolve_kto_area)해 areaBasedList2 호출.
    우선순위: areaBased(코드 도출 성공) > 좌표 기반 폴백(실패/결과 0).
    """
    lat = accommodation["latitude"]
    lng = accommodation["longitude"]

    items, source = [], ""

    # 1) 좌표 → KTO 코드 → 지역 대표 명소 (가장 정확)
    area_code, sigungu_code = await _resolve_kto_area(lat, lng)
    if area_code:
        try:
            items = await kto.area_based_list(
                area_code, sigungu_code,
                content_type_id=kto.CONTENT_TYPE["tourist_spot"],
                num_of_rows=20, arrange="P",
            )
            source = "KTO-areaBased"
        except Exception:
            items = []

    # 2) 폴백: 코드 도출 실패 또는 결과 0 → 좌표 반경 검색
    if not items:
        try:
            items = await kto.location_based_list(
                lat, lng, 20000,
                content_type_id=kto.CONTENT_TYPE["tourist_spot"],
            )
            source = "KTO-locationBased(fallback)"
        except Exception:
            return [], ""

    # 숙소로부터 직선 거리 부여 + 가까운 순 정렬
    for it in items:
        it.dist_meters = haversine_meters(lat, lng, it.latitude, it.longitude)
    items.sort(key=lambda x: x.dist_meters or 9e9)
    return items[:15], source


# ─────────────────────────────────────────────────────────────────────────────
# 좌표 → KTO 지역코드 변환 (KTO는 local만 쓰므로 변환도 local에서)
# ─────────────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _area_codes() -> dict:
    """build_area_codes.py가 생성한 코드 테이블 로드 (1회 캐싱)."""
    path = Path(__file__).resolve().parent.parent / "tools" / "area_codes.json"
    return json.loads(path.read_text(encoding="utf-8"))


# KTO/Kakao 시도명 표기 차이 흡수용 약칭 매핑
_SIDO_ALIAS = {
    "전북": "전라북도", "전남": "전라남도",
    "경북": "경상북도", "경남": "경상남도",
    "충북": "충청북도", "충남": "충청남도",
}


def _norm_sido(name: str) -> str:
    """시도명 정규화 → 약칭→전칭 후 접미사 제거."""
    name = _SIDO_ALIAS.get(name, name)
    for s in ("특별자치도", "특별자치시", "특별시", "광역시", "도"):
        if name.endswith(s):
            return name[: -len(s)]
    return name


async def _resolve_kto_area(lat: float, lng: float) -> tuple[str, Optional[str]]:
    """좌표 → KTO (area_code, sigungu_code).

    1) Kakao coord2regioncode로 시도·시군구 이름 획득
    2) area_codes.json에서 이름 매칭해 코드로 변환
    실패하면 ("", None) → 호출 측에서 좌표 폴백.
    """
    try:
        region = await kakao.coord2regioncode(lat, lng)
    except Exception:
        return ("", None)
    if region is None:
        return ("", None)

    try:
        codes = _area_codes()
    except Exception:
        return ("", None)

    target_sido = _norm_sido(region.sido)

    matched_sido = next(
        (c for c, info in codes.items() if _norm_sido(info["name"]) == target_sido),
        None,
    )
    if not matched_sido:
        return ("", None)

    # 시군구 매칭 (앞 2글자 기준, "남원시"→"남원")
    sg_q = region.sigungu[:2]
    for sg_code, sg_name in codes[matched_sido]["sigungu"].items():
        if sg_name.startswith(sg_q):
            return (matched_sido, sg_code)

    return (matched_sido, None)   # 시군구 못 찾으면 시도만


async def _maybe_blog_search(user_input: UserInput, accommodation: dict):
    """hobby/vibe 있을 때만 블로그 후기 호출."""
    hint = next(
        (h for h in (user_input.tourism_hobby, user_input.desired_vibe)
         if h and h.strip()),
        None,
    )
    if not hint:
        return []
    addr = (accommodation.get("address") or "").split(" ")
    # 주소 첫 번째 토큰이 "대한민국"이면 두 번째 사용
    region_token = addr[0] if addr else ""
    if region_token == "대한민국" and len(addr) > 1:
        region_token = addr[1]
    query = f"{region_token} {hint}".strip()
    try:
        return await naver.search_blog(query, display=5)
    except Exception:
        return []


async def _empty():
    return []


# ─────────────────────────────────────────────────────────────────────────────
# 반경 결정
# ─────────────────────────────────────────────────────────────────────────────
def _initial_radius_m(user_input: UserInput, retry: int) -> int:
    """이동수단·재호출 횟수에 따라 검색 반경(m) 결정.

    자동차의 경우 초기 반경은 SEARCH_RADIUS_CAR_M (15km)이며,
    재호출 시 +RETRY_CAR_EXPAND_M (5km)씩 확장되어 API 한도 20km까지 도달.
    """
    transport = (user_input.transport or "").strip().lower()
    is_walk = transport in {"도보", "walk", "walking", "걸어서"}

    if is_walk:
        return SEARCH_RADIUS_LOCAL_M + RETRY_RADIUS_EXPAND_M * retry
    else:
        return SEARCH_RADIUS_CAR_M + RETRY_CAR_EXPAND_M * retry
