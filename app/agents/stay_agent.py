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
from app.tools.kto import search_accommodations as kto_search_accommodations, simplify_accommodation, get_accommodation_price
from app.tools.kakao import search_accommodations as kakao_search_accommodations
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

async def _region_exists(region_name: str) -> bool:
    """VWorld 행정구역 DB로 지역명이 실제 존재하는지 검증한다.
    시군구 코드 + 읍면동/리 코드를 조합해 정확한 위치 검증.
    """
    import httpx
    from app.config.settings import VWORLD_API_KEY, VWORLD_BASE_URL

    cleaned = region_name.replace("생활권", "").strip()
    words = cleaned.split()

    async def _get_codes(data: str, field: str, value: str, code_field: str) -> list[str]:
        params = {"service": "data", "request": "GetFeature", "data": data,
                  "key": VWORLD_API_KEY, "domain": "localhost",
                  "attrFilter": f"{field}:like:{value}", "format": "json", "size": "5"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(VWORLD_BASE_URL, params=params)
                body = r.json().get("response", {})
                if body.get("status") != "OK":
                    return []
                features = body.get("result", {}).get("featureCollection", {}).get("features", [])
                return [f["properties"].get(code_field, "") for f in features if f["properties"].get(code_field)]
        except Exception:
            return []

    suffixes_emd = ("읍", "면", "동")
    suffixes_ri  = ("리",)
    suffixes_sig = ("시", "군", "구")

    emd_words = [w for w in words if any(w.endswith(s) for s in suffixes_emd) and len(w) >= 2]
    ri_words  = [w for w in words if any(w.endswith(s) for s in suffixes_ri)  and len(w) >= 2]
    sig_words = [w for w in words if any(w.endswith(s) for s in suffixes_sig) and len(w) >= 2]

    # 시/군/구 코드 수집
    sig_codes: list[str] = []
    for sw in sig_words:
        sig_codes.extend(await _get_codes("LT_C_ADSIGG_INFO", "sig_kor_nm", sw, "sig_cd"))

    # 읍/면/동 검증: 해당 코드가 시군구 코드 범위 내에 있는지 확인
    for word in emd_words:
        emd_codes = await _get_codes("LT_C_ADEMD_INFO", "emd_kor_nm", word, "emd_cd")
        if not emd_codes:
            continue
        if not sig_codes:
            return True  # 시군구 정보 없으면 전국 존재 여부만 확인
        if any(emd_cd[:5] == sig_cd for emd_cd in emd_codes for sig_cd in sig_codes):
            return True

    # 리 검증
    for word in ri_words:
        ri_codes = await _get_codes("LT_C_ADRI_INFO", "li_kor_nm", word, "ri_cd")
        if not ri_codes:
            continue
        if not sig_codes:
            return True
        if any(ri_cd[:5] == sig_cd for ri_cd in ri_codes for sig_cd in sig_codes):
            return True

    # 읍면동/리 없으면 시군구만 확인
    if not emd_words and not ri_words:
        if sig_codes:
            return True
        # 접미사 없는 단어도 시/군/구 이름으로 시도 (예: "남원" → "남원시" 검색)
        for word in words:
            if len(word) >= 2:
                codes = await _get_codes("LT_C_ADSIGG_INFO", "sig_kor_nm", word, "sig_cd")
                if codes:
                    return True
        return False

    return False


async def _generate_candidates(state: dict, parsed: dict, extracted: str,
                               exclude_names: list[str], count: int) -> list[dict]:
    """LLM으로 후보 지역을 생성한다. 이미 검증 실패한 지역은 제외 목록에 추가."""
    excluded = list(state.get("excluded_regions", [])) + exclude_names
    text = await call_llm(
        messages=[{
            "role": "user",
            "content": REGION_SEARCH_USER.format(
                excluded_regions="\n".join(excluded) or "없음",
                must_have="\n".join(state.get("must_have_conditions", [])),
                preference="\n".join(state.get("preference_conditions", [])),
                avoid="\n".join(state.get("avoid_conditions", [])),
                priority_weights=json.dumps(state.get("priority_weights", {}), ensure_ascii=False),
                naver_reviews=extracted or "검색 결과 없음",
                parsed_preferences=json.dumps(parsed, ensure_ascii=False, indent=2),
            ).replace("정확히 3개", f"정확히 {count}개"),
        }],
        system=REGION_SEARCH_SYSTEM,
        max_tokens=2000,
    )
    candidates = json.loads(text)
    return candidates if isinstance(candidates, list) else []


@traceable(name="stay_region_search")
async def region_search_node(state: dict[str, Any]) -> dict[str, Any]:
    import asyncio as _asyncio

    parsed = state.get("parsed_preferences", {})

    # Step 1: 네이버 후기 수집 + 첫 LLM 후보 생성 병렬 실행
    # (후기 없이도 LLM이 조건 기반으로 후보를 생성할 수 있으므로 병렬 가능)
    naver_task = search_region_reviews(parsed)
    first_candidates_task = _generate_candidates(state, parsed, "", [], 4)

    naver_reviews, first_candidates = await _asyncio.gather(
        naver_task, first_candidates_task, return_exceptions=True
    )
    naver_reviews = naver_reviews if isinstance(naver_reviews, str) else ""
    first_candidates = first_candidates if isinstance(first_candidates, list) else []

    # 후기로 보강된 후보가 필요하면 추가 추출 (빠른 버전: 병렬이었으므로 후기가 있으면 재사용)
    extracted = await _extract_region_insights(naver_reviews) if naver_reviews else ""

    # Step 2: Kakao 검증 → 3개 확보될 때까지 반복 (첫 후보는 이미 생성됨)
    validated: list[dict] = []
    failed_names: list[str] = []
    max_attempts = 3
    pending_candidates = first_candidates  # 첫 번째 후보 재사용

    for attempt in range(max_attempts):
        needed = 3 - len(validated)
        if needed <= 0:
            break

        candidates = pending_candidates if pending_candidates else \
                     await _generate_candidates(state, parsed, extracted, failed_names, needed + 1)
        pending_candidates = []  # 다음 루프에서는 새로 생성

        # 각 후보를 Kakao로 검증 (병렬) — 지역 존재 여부만 확인
        results = await _asyncio.gather(
            *[_region_exists(c.get("region_name", "")) for c in candidates],
            return_exceptions=True,
        )

        excluded_regions: list[str] = state.get("excluded_regions", [])

        for candidate, has_acc in zip(candidates, results):
            if len(validated) >= 3:
                break
            name = candidate.get("region_name", "")
            name_clean = name.replace("생활권", "").strip()

            # excluded_regions 코드 레벨 차단 — LLM이 프롬프트 무시해도 통과 불가
            if any(ex.strip() in name_clean or name_clean in ex.strip()
                   for ex in excluded_regions if ex.strip()):
                failed_names.append(name)
                continue

            # 마지막 단어(읍/면/동/리) 기준 중복 제거
            specific = name_clean.split()[-1] if name_clean else ""
            already_seen = any(
                v.get("region_name", "").replace("생활권", "").strip().split()[-1] == specific
                for v in validated
            )
            if already_seen:
                failed_names.append(name)
                continue
            if has_acc is True:
                validated.append(candidate)
            else:
                failed_names.append(name)

    # 검증된 지역 순위 재정렬
    for i, region in enumerate(validated):
        region["rank"] = i + 1

    return {"candidate_regions": validated}


# ── Phase 2: 숙소 탐색 & 점수화 ───────────────────────────────

def _merge_accommodations(kto_items: list[dict], kakao_items: list) -> list[dict]:
    """KTO + Kakao 결과 병합 및 중복 제거."""
    merged: list[dict] = list(kto_items)
    kto_names = {item.get("title", "").strip().lower() for item in kto_items}

    for place in kakao_items:
        name = place.name.strip().lower()
        if any(name in kn or kn in name for kn in kto_names):
            continue
        kto_names.add(name)
        merged.append({
            "contentid": f"kakao_{place.place_id}",
            "title":     place.name,
            "addr1":     place.address or place.road_address or "",
            "addr2":     "",
            "cat3":      "숙박",
            "mapx":      str(place.longitude) if place.longitude else None,
            "mapy":      str(place.latitude)  if place.latitude  else None,
            "firstimage": None,
            "homepage":   place.place_url,
            "tel":        place.phone,
        })

    return merged


@traceable(name="stay_accommodation_search")
async def accommodation_search_node(state: dict[str, Any]) -> dict[str, Any]:
    import asyncio as _asyncio

    selected: dict = state["selected_region"]
    region_name: str = selected.get("region_name", "")
    parsed = state.get("parsed_preferences", {})
    must_have = state.get("must_have_conditions", [])

    # KTO + Kakao 동시 검색
    kto_result, kakao_result = await _asyncio.gather(
        kto_search_accommodations(region_name),
        kakao_search_accommodations(region_name, max_results=20),
        return_exceptions=True,
    )

    kto_items   = kto_result   if isinstance(kto_result, list)   else []
    kakao_items = kakao_result if isinstance(kakao_result, list) else []
    raw_items   = _merge_accommodations(kto_items, kakao_items)

    if not raw_items:
        return {
            "candidate_accommodations": [],
            "warnings": [f"{region_name} 숙소 검색 결과가 없습니다."],
        }

    # 숙소별 네이버 후기 수집
    # LLM에 너무 많은 숙소를 넘기면 느려지므로 상위 15개만 처리
    raw_items = raw_items[:15]

    # 숙소 정보 준비 + Naver 후기 + KTO 가격 병렬 수집
    base_items = [simplify_accommodation(item) for item in raw_items]
    reviews_and_prices = await _asyncio.gather(
        *[search_reviews(acc["name"]) for acc in base_items],
        *[get_accommodation_price(acc["id"]) for acc in base_items],
        return_exceptions=True,
    )
    n = len(base_items)
    reviews_list = reviews_and_prices[:n]
    prices_list  = reviews_and_prices[n:]

    simplified = []
    for acc, review, price in zip(base_items, reviews_list, prices_list):
        acc["reviews"]    = review if isinstance(review, str) else ""
        acc["price"] = price if isinstance(price, str) else None
        simplified.append(acc)

    # LLM에는 id를 숨기고 index만 부여 — ID 환각 원천 차단
    indexed = []
    for i, acc in enumerate(simplified):
        entry = {k: v for k, v in acc.items() if k != "id"}
        entry["index"] = i + 1
        indexed.append(entry)

    # LLM으로 점수화 → 상위 3개 선별
    text = await call_llm(
        messages=[{
            "role": "user",
            "content": ACCOMMODATION_SCORE_USER.format(
                region_name=region_name,
                parsed_preferences=json.dumps(parsed, ensure_ascii=False, indent=2),
                must_have="\n".join(must_have),
                total_count=len(indexed),
                accommodations_with_reviews=json.dumps(indexed, ensure_ascii=False, indent=2),
            ),
        }],
        system=ACCOMMODATION_SCORE_SYSTEM,
        max_tokens=3000,
    )

    llm_result: list[dict] = json.loads(text)

    # index → 실제 데이터 매핑 (LLM이 생성한 ID/이름 완전 무시)
    index_to_simplified = {i + 1: acc        for i, acc in enumerate(simplified)}
    index_to_raw        = {i + 1: raw_items[i] for i in range(len(simplified))}

    verified = []
    for r in llm_result:
        idx = r.get("index")
        if not isinstance(idx, int) or idx not in index_to_simplified:
            continue  # 범위 벗어난 index → 환각, 무시
        real_acc = index_to_simplified[idx]
        real_raw = index_to_raw[idx]
        r["id"]      = real_acc["id"]
        r["name"]    = real_raw.get("title", real_acc.get("name", ""))
        r["address"] = f"{real_raw.get('addr1','')} {real_raw.get('addr2','')}".strip()
        verified.append(r)

    if not verified:
        return {
            "candidate_accommodations": [],
            "warnings": [f"{region_name} 지역에서 실제 숙소를 찾지 못했습니다. 다른 지역을 선택해주세요."],
        }
    ranked = verified

    # 원본 데이터에서 좌표·이미지·연락처 보강
    # id는 이미 index 매핑으로 실제값이 보장되므로 직접 조회 가능
    contentid_to_raw = {str(item.get("contentid")): item for item in raw_items}
    for item in ranked:
        raw = contentid_to_raw.get(str(item.get("id")), {})
        item["image_url"] = raw.get("firstimage") or None
        item["homepage"]  = raw.get("homepage") or None
        item["tel"]       = raw.get("tel") or None
        item["mapx"]      = raw.get("mapx") or None
        item["mapy"]      = raw.get("mapy") or None

    return {"candidate_accommodations": ranked}
