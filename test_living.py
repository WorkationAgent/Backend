"""
Living Agent 단계별 테스트.

Backend/ 디렉터리에서 실행:
  python test_living.py

단계:
  1. geocode_address  — 주소 → 좌표 (Kakao Local)
  2. quick_scan       — 주변 인프라 현황 (Naver)
  3. Directions API   — 도로 거리/시간 (Kakao Mobility)
  4. search_living_infra — 전체 Tool 파이프라인
  5. living_agent     — 전체 Agent 파이프라인 (LLM 포함)
"""

import asyncio
import json
import sys

# ── 테스트 대상 숙소 (강릉 워케이션 예시) ─────────────────────────────────────
TEST_ADDRESS = "강원도 강릉시 강릉대로 33"   # 강릉시청
TEST_LAT     = 37.7519
TEST_LNG     = 128.8761

TEST_ACCOMMODATION = {
    "id": "test-gangneung-001",
    "name": "강릉 테스트 숙소",
    "address": TEST_ADDRESS,
    "latitude": TEST_LAT,
    "longitude": TEST_LNG,
}

TEST_STATE = {
    "candidate_accommodations": [TEST_ACCOMMODATION],
    "parsed_preferences": {
        "transport": "뚜벅이",
        "duration": "14일",
        "living_infra": "마트, 편의점, 약국",
        "desired_region": "강릉",
    },
    "priority_weights": {
        "transport": 0.35,
        "grocery": 0.30,
        "medical": 0.20,
        "services": 0.15,
    },
    "retry_count": {},
    "errors": [],
}


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def ok(msg: str) -> None:
    print(f"  ✓  {msg}")

def fail(msg: str) -> None:
    print(f"  ✗  {msg}")
    sys.exit(1)


# ── Step 1: geocode_address ───────────────────────────────────────────────────

async def test_geocode() -> None:
    section("Step 1. geocode_address — 주소 → 좌표")
    import httpx
    from app.config.settings import KAKAO_LOCAL_URL, KAKAO_REST_API_KEY

    params  = {"query": TEST_ADDRESS, "size": 1}
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{KAKAO_LOCAL_URL}/search/address.json",
            params=params, headers=headers, timeout=5.0,
        )

    print(f"  HTTP {resp.status_code}")
    print(f"  응답: {resp.text[:500]}")

    if resp.status_code != 200:
        fail(f"API 오류 — 키 또는 URL 확인 필요")

    docs = resp.json().get("documents", [])
    if not docs:
        fail("결과 없음 — 주소를 찾을 수 없음")

    doc = docs[0]
    ok(f"lat={doc['y']}, lng={doc['x']}")


# ── Step 2: quick_scan ────────────────────────────────────────────────────────

async def test_quick_scan() -> None:
    section("Step 2. quick_scan — Kakao 주변 인프라 현황")
    from app.tools.living_tool import quick_scan

    result = await quick_scan(TEST_LAT, TEST_LNG, scan_radius_km=3.0)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    found_any = any(v.get("found") for k, v in result.items() if k != "scan_radius_km")
    if not found_any:
        print("  ⚠  반경 내 결과 없음 — API 키 또는 좌표 확인 필요")
    else:
        ok("Kakao 검색 정상 응답")


# ── Step 3: Directions API ────────────────────────────────────────────────────

async def test_directions() -> None:
    section("Step 3. Directions API — 도로 거리/소요 시간")
    import httpx
    from app.config.settings import KAKAO_MOBILITY_URL, KAKAO_REST_API_KEY

    # 강릉 시청까지 테스트
    dest_lat, dest_lng = 37.7513, 128.8760
    params  = {
        "origin":      f"{TEST_LNG},{TEST_LAT}",
        "destination": f"{dest_lng},{dest_lat}",
        "priority":    "RECOMMEND",
    }
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(KAKAO_MOBILITY_URL, params=params, headers=headers, timeout=5.0)

    if resp.status_code != 200:
        fail(f"Directions API 오류: {resp.status_code} — Kakao Mobility 활성화 확인 필요\n{resp.text[:300]}")

    routes = resp.json().get("routes", [])
    if not routes or routes[0].get("result_code") != 0:
        fail(f"경로 없음: {resp.json()}")

    s = routes[0]["summary"]
    ok(f"도로 거리: {s['distance']}m")
    ok(f"소요 시간: {s['duration']}초 ({s['duration']//60}분)")


# ── Step 4: search_living_infra (Tool 전체) ───────────────────────────────────

async def test_tool() -> None:
    section("Step 4. search_living_infra — Tool 전체 파이프라인")
    from app.tools.living_tool import search_living_infra
    from app.schemas.living_schema import (
        CategorySearchPlan, LivingSearchPlan,
    )

    # Planning LLM 없이 직접 plan 구성
    mock_plan = LivingSearchPlan(
        transport=CategorySearchPlan(
            kakao_codes=["BS8"],
            kakao_keywords=["버스정류장", "기차역"],
            naver_keywords=["시외버스", "KTX"],
            weight=0.35,
            priority="preferred",
        ),
        grocery=CategorySearchPlan(
            kakao_codes=["MT1", "CS2"],
            kakao_keywords=[],
            naver_keywords=["마트", "슈퍼"],
            weight=0.30,
            priority="essential",
        ),
        medical=CategorySearchPlan(
            kakao_codes=["HP8", "PM9"],
            kakao_keywords=[],
            naver_keywords=["의원", "한의원"],
            weight=0.20,
            priority="preferred",
        ),
        services=CategorySearchPlan(
            kakao_codes=["BK9"],
            kakao_keywords=[],
            naver_keywords=["은행", "ATM"],
            weight=0.15,
            priority="optional",
        ),
        primary_radius_km=1.5,
        retry_radius_km=2.5,
        reasoning="테스트용 mock plan",
    )

    details = await search_living_infra(TEST_LAT, TEST_LNG, mock_plan, transport_mode="walk")
    print(json.dumps(details.model_dump(), ensure_ascii=False, indent=2))
    ok("Tool 파이프라인 정상 실행")


# ── Step 5: living_agent (전체 파이프라인) ────────────────────────────────────

async def test_agent() -> None:
    section("Step 5. living_agent — 전체 파이프라인 (LLM 포함)")
    from app.agents.living_agent import living_agent

    result = await living_agent(TEST_STATE)

    evaluations = result.get("living_evaluations", [])
    errors      = result.get("errors", [])

    if errors:
        print(f"  ⚠  errors: {errors}")

    if not evaluations:
        fail("living_evaluations 비어있음")

    for ev in evaluations:
        print(f"\n  숙소: {ev.accommodation_id}")
        print(f"  score:      {ev.score}")
        print(f"  confidence: {ev.confidence}")
        print(f"  summary:    {ev.summary}")
        if ev.details:
            for cat in ("transport", "grocery", "medical", "services"):
                d = ev.details.get(cat, {})
                print(f"  [{cat}] found={d.get('found')} zone={d.get('zone_km')}km "
                      f"nearest={d.get('nearest_minutes')}min source={d.get('source')}")

    ok("Agent 파이프라인 정상 완료")


# ── 실행 ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    steps = {
        "1": ("geocode",    test_geocode),
        "2": ("quick_scan", test_quick_scan),
        "3": ("directions", test_directions),
        "4": ("tool",       test_tool),
        "5": ("agent",      test_agent),
    }

    # 인자 없으면 전체 실행, 숫자 인자 있으면 해당 step만
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(steps.keys())

    for key in targets:
        if key not in steps:
            print(f"알 수 없는 step: {key}  (1~5 중 선택)")
            continue
        _, fn = steps[key]
        try:
            await fn()
        except SystemExit:
            raise
        except Exception as e:
            fail(f"예외 발생: {e}")

    print(f"\n{'='*60}")
    print("  테스트 완료")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
