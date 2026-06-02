from __future__ import annotations
import asyncio
from typing import Optional

from app.config.settings import (
    SEARCH_RADIUS_LOCAL_M,
    SEARCH_RADIUS_CAR_M,
    RETRY_CONFIDENCE_THRESHOLD,
    RETRY_RADIUS_EXPAND_M,
    RETRY_CAR_EXPAND_M,
    RETRY_MAX_COUNT,
)
from app.core.llm import call_llm
from app.prompts.local_agent import SYSTEM_PROMPT, build_user_prompt
from app.schemas.user_input import UserInput
from app.schemas.worker import LocalEvaluation
from app.tools import kto, kakao, naver
from app.tools.rag import retrieve_regional_context


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
async def evaluate_accommodations(
    accommodations: list[dict],
    user_input: UserInput,
    stay_dates: Optional[tuple[str, str]] = None,
) -> list[LocalEvaluation]:
    """Stay Agent가 넘긴 숙소 3개를 병렬 평가."""
    tasks = [
        _evaluate_one(acc, user_input, stay_dates)
        for acc in accommodations
    ]
    return await asyncio.gather(*tasks)


# ─────────────────────────────────────────────────────────────────────────────
# 숙소 1개 평가
# ─────────────────────────────────────────────────────────────────────────────
async def _evaluate_one(
    accommodation: dict,
    user_input: UserInput,
    stay_dates: Optional[tuple[str, str]],
    retry: int = 0,
) -> LocalEvaluation:
    radius_m = _initial_radius_m(user_input, retry)

    # 1) 신호 수집 (병렬)
    places, festivals, blog_snippets, regional_context = await _collect_signals(
        accommodation, user_input, stay_dates, radius_m
    )

    # 결과 0건 → 반경 확장 후 즉시 재호출 (LLM 비용 절약)
    total = sum(len(v) for v in places.values()) + len(festivals)
    if total == 0 and retry < RETRY_MAX_COUNT:
        return await _evaluate_one(accommodation, user_input, stay_dates, retry + 1)

    # 2) LLM 평가
    user_msg = build_user_prompt(
        accommodation=accommodation,
        user_input=user_input.model_dump(),
        places={k: [p.model_dump() for p in v] for k, v in places.items()},
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

    # 3) confidence 부족 → 재호출
    if (
        evaluation.confidence is not None
        and evaluation.confidence <= RETRY_CONFIDENCE_THRESHOLD
        and retry < RETRY_MAX_COUNT
    ):
        return await _evaluate_one(accommodation, user_input, stay_dates, retry + 1)

    return evaluation


# ─────────────────────────────────────────────────────────────────────────────
# 신호 수집
# ─────────────────────────────────────────────────────────────────────────────
async def _collect_signals(
    accommodation: dict,
    user_input: UserInput,
    stay_dates: Optional[tuple[str, str]],
    radius_m: int,
):
    lat = accommodation["latitude"]
    lng = accommodation["longitude"]
    region = accommodation.get("region", "")

    # KTO 기본 4종
    kto_tasks = [
        kto.location_based_list(lat, lng, radius_m, kto.CONTENT_TYPE["tourist_spot"]),
        kto.location_based_list(lat, lng, radius_m, kto.CONTENT_TYPE["cultural"]),
        kto.location_based_list(lat, lng, radius_m, kto.CONTENT_TYPE["leports"]),
        kto.location_based_list(lat, lng, radius_m, kto.CONTENT_TYPE["shopping"]),
    ]

    # 축제: stay_dates가 있을 때만
    festival_task = (
        kto.search_festival(lat, lng, radius_m, stay_dates[0], stay_dates[1])
        if stay_dates else _empty()
    )

    # Kakao 보강
    kakao_tasks = [
        kakao.category_search(lat, lng, radius_m, "AT4"),
        kakao.category_search(lat, lng, radius_m, "CE7"),
    ]

    # Naver 블로그 (hobby/vibe 있을 때만)
    blog_task = _maybe_blog_search(user_input, accommodation)

    # RAG (v1은 stub이라 항상 빈 리스트 반환)
    rag_task = retrieve_regional_context(
        region=region,
        user_hints=[
            user_input.tourism_hobby or "",
            user_input.desired_vibe or "",
            user_input.region_style or "",
        ],
        top_k=5,
    )

    (tourist, cultural, leports, shopping,
     festivals,
     kakao_tour, kakao_cafe,
     blog_snippets,
     regional_context) = await asyncio.gather(
        *kto_tasks, festival_task, *kakao_tasks, blog_task, rag_task
    )

    places = {
        "kto_tourist_spots": tourist,
        "kto_cultural":      cultural,
        "kto_leports":       leports,
        "kto_shopping":      shopping,
        "kakao_tourist":     kakao_tour,
        "kakao_vibe_cafe":   kakao_cafe,
    }
    return places, festivals, blog_snippets, regional_context


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
    region = addr[0] if addr else ""
    query = f"{region} {hint}".strip()
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
