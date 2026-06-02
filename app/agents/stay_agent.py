"""
Stay Agent

Phase 1 - region_search_node:
    읽기: must_have_conditions, avoid_conditions, preference_conditions,
           priority_weights, parsed_preferences
    쓰기: candidate_regions

Phase 2 - accommodation_search_node:
    읽기: selected_region, parsed_preferences, must_have_conditions
    쓰기: candidate_accommodations
"""

import json
from typing import Any

from langsmith import traceable

from app.core.llm import call_llm
from app.prompts.stay_prompts import (
    ACCOMMODATION_SCORE_SYSTEM,
    ACCOMMODATION_SCORE_USER,
    NAVER_EXTRACT_SYSTEM,
    NAVER_EXTRACT_USER,
    REGION_SEARCH_SYSTEM,
    REGION_SEARCH_USER,
)
from app.tools.kto import search_accommodations, simplify_accommodation
from app.tools.naver import search_region_reviews, search_accommodation_reviews as search_reviews


# ── 내부 헬퍼 ──────────────────────────────────────────────────

@traceable(name="extract_region_insights")
async def _extract_region_insights(naver_reviews: str) -> str:
    """네이버 후기에서 지역별 핵심 정보를 추출한다."""
    if not naver_reviews:
        return ""
    return await call_llm(
        messages=[{"role": "user", "content": NAVER_EXTRACT_USER.format(naver_reviews=naver_reviews)}],
        system=NAVER_EXTRACT_SYSTEM,
        max_tokens=1000,
    )


# ── Phase 1: 후보 생활권 탐색 ──────────────────────────────────

@traceable(name="stay_region_search")
async def region_search_node(state: dict[str, Any]) -> dict[str, Any]:
    parsed = state.get("parsed_preferences", {})

    # Step 1: 네이버 후기 수집 → LLM이 지역별 핵심 정보 추출
    naver_reviews = await search_region_reviews(parsed)
    extracted = await _extract_region_insights(naver_reviews)

    # Step 2: 추출된 정보 + 사용자 조건 → 생활권 3개 추천
    text = await call_llm(
        messages=[{
            "role": "user",
            "content": REGION_SEARCH_USER.format(
                must_have="\n".join(state.get("must_have_conditions", [])),
                preference="\n".join(state.get("preference_conditions", [])),
                avoid="\n".join(state.get("avoid_conditions", [])),
                priority_weights=json.dumps(state.get("priority_weights", {}), ensure_ascii=False),
                naver_reviews=extracted or "검색 결과 없음",
                parsed_preferences=json.dumps(parsed, ensure_ascii=False, indent=2),
            ),
        }],
        system=REGION_SEARCH_SYSTEM,
        max_tokens=2000,
    )

    raw: list[dict] = json.loads(text)
    raw.sort(key=lambda x: x.get("rank", 99))

    return {"candidate_regions": raw}


# ── Phase 2: 숙소 탐색 & 점수화 ───────────────────────────────

@traceable(name="stay_accommodation_search")
async def accommodation_search_node(state: dict[str, Any]) -> dict[str, Any]:
    selected: dict = state["selected_region"]
    region_name: str = selected.get("region_name", "")
    parsed = state.get("parsed_preferences", {})
    must_have = state.get("must_have_conditions", [])

    # KTO API로 숙소 검색
    raw_items = await search_accommodations(region_name)
    if not raw_items:
        return {
            "candidate_accommodations": [],
            "warnings": [f"{region_name} 반경 내 숙소 검색 결과가 없습니다. 반경을 직접 확장하거나 다른 지역을 선택해주세요."],
        }

    # 숙소별 네이버 후기 수집
    simplified = []
    for item in raw_items:
        acc = simplify_accommodation(item)
        acc["reviews"] = await search_reviews(acc["name"])
        simplified.append(acc)

    # LLM으로 점수화 → 상위 3개 선별
    text = await call_llm(
        messages=[{
            "role": "user",
            "content": ACCOMMODATION_SCORE_USER.format(
                region_name=region_name,
                parsed_preferences=json.dumps(parsed, ensure_ascii=False, indent=2),
                must_have="\n".join(must_have),
                accommodations_with_reviews=json.dumps(simplified, ensure_ascii=False, indent=2),
            ),
        }],
        system=ACCOMMODATION_SCORE_SYSTEM,
        max_tokens=3000,
    )

    ranked: list[dict] = json.loads(text)

    # KTO 원본 데이터에서 좌표·이미지·연락처 보강
    id_to_raw = {str(item.get("contentid")): item for item in raw_items}
    for item in ranked:
        raw = id_to_raw.get(str(item.get("id")), {})
        item["image_url"] = raw.get("firstimage") or None
        item["homepage"] = raw.get("homepage") or None
        item["tel"] = raw.get("tel") or None
        item["mapx"] = raw.get("mapx") or None
        item["mapy"] = raw.get("mapy") or None

    return {"candidate_accommodations": ranked}
