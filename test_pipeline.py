"""
Stay → Living + Local 통합 파이프라인 테스트
Work Agent는 미구현으로 제외

실행:
  python test_pipeline.py
"""

import asyncio
import json
import sys

from app.agents.stay_agent import region_search_node, accommodation_search_node
from app.agents.living_agent import living_agent
from app.agents.local_agent import evaluate_accommodations
from app.agents.work_agent import work_agent
from app.schemas.user_input import UserInput

# ── 가짜 Planner 출력 데이터 ───────────────────────────────────
FAKE_STATE = {
    "parsed_preferences": {
        "purpose": "워케이션",
        "duration": "14일",
        "desired_region": "제주 또는 바다 근처",
        "region_style": "바다, 감성동네",
        "desired_vibe": "조용한, 자연친화",
        "tourism_hobby": "카페, 산책, 맛집탐방",
        "work_required": True,
        "work_style": "카페 작업, 숙소 작업",
        "transport": "뚜벅이",
        "travel_distance": "도보 15분 이내",
        "living_infra": "마트, 편의점, 병원",
        "budget": "중간",
        "accommodation_style": "감성숙소, 가성비",
        "companion": "혼자",
        "priority": "작업환경 > 생활인프라 > 교통 > 숙소 > 관광",
        "additional_request": "너무 관광지 느낌은 싫고 조용하지만 카페가 어느 정도 있으면 좋겠어요",
    },
    "must_have_conditions": [
        "작업 가능한 환경 (카페 또는 숙소)",
        "기본 생활 인프라 접근성 (마트, 편의점)",
        "도보 또는 대중교통으로 생활 가능",
    ],
    "preference_conditions": [
        "바다 근처", "조용한 분위기", "카페 접근성", "산책 가능한 환경", "감성숙소",
    ],
    "avoid_conditions": [
        "너무 관광지화된 지역", "혼잡한 지역",
    ],
    "priority_weights": {
        "work": 0.30,
        "living": 0.25,
        "transport": 0.20,
        "accommodation": 0.15,
        "local": 0.10,
    },
    "retry_count": {},
    "errors": [],
}


def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _to_float(val) -> float | None:
    try:
        return float(val) if val else None
    except (TypeError, ValueError):
        return None


def _build_accommodation_list(candidate_accommodations: list[dict]) -> list[dict]:
    """Stay Agent 출력(mapx/mapy)을 Living/Local Agent 입력(latitude/longitude)으로 변환."""
    result = []
    for acc in candidate_accommodations:
        lat = _to_float(acc.get("mapy"))
        lng = _to_float(acc.get("mapx"))
        if not lat or not lng:
            continue
        result.append({
            "id": acc.get("id", ""),
            "name": acc.get("name", ""),
            "address": acc.get("address", ""),
            "latitude": lat,
            "longitude": lng,
            "region": FAKE_STATE["parsed_preferences"].get("desired_region", ""),
        })
    return result


# ── Phase 1: 생활권 탐색 ───────────────────────────────────────

async def run_stay_phase1() -> list[dict]:
    section("Phase 1. Stay Agent — 생활권 탐색")
    result = await region_search_node(FAKE_STATE)
    candidates = result["candidate_regions"]

    for c in candidates:
        print(f"\n  [{c['rank']}순위] {c['region_name']} ({c['initial_fit_score']}점)")
        print(f"  이유: {c['brief_reason'][:60]}...")

    print(f"\n  → 1순위 자동 선택: {candidates[0]['region_name']}")
    return candidates


# ── Phase 2: 숙소 탐색 ────────────────────────────────────────

async def run_stay_phase2(selected_region: dict) -> list[dict]:
    section("Phase 2. Stay Agent — 숙소 탐색 & 점수화")
    state = {**FAKE_STATE, "selected_region": selected_region}
    result = await accommodation_search_node(state)

    accommodations = result.get("candidate_accommodations", [])
    warnings = result.get("warnings", [])

    if warnings:
        print(f"  ⚠  {warnings}")

    for a in accommodations:
        print(f"\n  [{a['rank']}위] {a['name']} ({a['total_score']}점)")
        print(f"  주소: {a.get('address', '')}")
        print(f"  좌표: ({a.get('mapy')}, {a.get('mapx')})")

    return accommodations


# ── Living Agent ──────────────────────────────────────────────

async def run_living(accommodations: list[dict]) -> None:
    section("Living Agent — 생활 인프라 평가")

    state = {
        **FAKE_STATE,
        "candidate_accommodations": accommodations,
    }

    result = await living_agent(state)
    evaluations = result.get("living_evaluations", [])
    errors = result.get("errors", [])

    if errors:
        print(f"  ⚠  errors: {errors}")

    for ev in evaluations:
        print(f"\n  숙소: {ev.accommodation_id}")
        print(f"  score:      {ev.score}")
        print(f"  confidence: {ev.confidence}")
        print(f"  summary:    {ev.summary}")


# ── Work Agent ────────────────────────────────────────────────

async def run_work(accommodations: list[dict], selected_region: dict) -> None:
    section("Work Agent — 업무 환경 평가")

    user_input = UserInput(
        purpose=FAKE_STATE["parsed_preferences"].get("purpose"),
        duration=FAKE_STATE["parsed_preferences"].get("duration"),
        desired_region=FAKE_STATE["parsed_preferences"].get("desired_region"),
        work_required=FAKE_STATE["parsed_preferences"].get("work_required"),
        work_style=FAKE_STATE["parsed_preferences"].get("work_style"),
        transport=FAKE_STATE["parsed_preferences"].get("transport"),
    )

    state = {
        **FAKE_STATE,
        "candidate_accommodations": accommodations,
        "user_input": user_input,
        "selected_region": selected_region,
    }

    result = await work_agent(state)
    evaluations = result.get("work_evaluations", [])
    errors = result.get("errors", [])

    if errors:
        print(f"  ⚠  errors: {errors}")

    for ev in evaluations:
        print(f"\n  숙소: {ev.accommodation_id}")
        print(f"  score:      {ev.score}")
        print(f"  confidence: {ev.confidence}")
        print(f"  summary:    {ev.summary}")


# ── Local Agent ───────────────────────────────────────────────

async def run_local(accommodations: list[dict]) -> None:
    section("Local Agent — 로컬 경험 평가")

    user_input = UserInput(
        purpose=FAKE_STATE["parsed_preferences"].get("purpose"),
        duration=FAKE_STATE["parsed_preferences"].get("duration"),
        desired_region=FAKE_STATE["parsed_preferences"].get("desired_region"),
        desired_vibe=FAKE_STATE["parsed_preferences"].get("desired_vibe"),
        tourism_hobby=FAKE_STATE["parsed_preferences"].get("tourism_hobby"),
        transport=FAKE_STATE["parsed_preferences"].get("transport"),
        companion=FAKE_STATE["parsed_preferences"].get("companion"),
    )

    results = await evaluate_accommodations(accommodations, user_input)

    for r in results:
        print(f"\n  숙소: {r.accommodation_id}")
        print(f"  score:      {r.score}")
        print(f"  confidence: {r.confidence}")
        print(f"  summary:    {r.summary}")


# ── 메인 ──────────────────────────────────────────────────────

async def main() -> None:
    # Stay Phase 1
    candidates = await run_stay_phase1()
    selected = candidates[0]

    # Stay Phase 2
    raw_accommodations = await run_stay_phase2(selected)

    if not raw_accommodations:
        print("\n  숙소 결과 없음 — 테스트 종료")
        sys.exit(1)

    # mapx/mapy → latitude/longitude 변환
    accommodations = _build_accommodation_list(raw_accommodations)

    if not accommodations:
        print("\n  유효한 좌표가 있는 숙소 없음 — 테스트 종료")
        sys.exit(1)

    # Living → Work → Local 순차 실행 (stdout 섞임 방지)
    # Living/Local: latitude/longitude 변환된 accommodations
    # Work: mapx/mapy 원본 raw_accommodations
    for coro in [
        run_living(accommodations),
        run_work(raw_accommodations, selected),
        run_local(accommodations),
    ]:
        try:
            await coro
        except Exception as e:
            print(f"\n  ⚠  Agent 에러: {e}")

    section("전체 파이프라인 테스트 완료")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
