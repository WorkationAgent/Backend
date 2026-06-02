"""
Stay → Living + Local 통합 파이프라인 테스트
Work Agent는 미구현으로 제외

실행:
  python test_pipeline.py
"""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.stay_agent import region_search_node, accommodation_search_node
from app.agents.living_agent import living_agent
from app.agents.local_agent import evaluate_accommodations
from app.agents.work_agent import work_agent
from app.schemas.user_input import UserInput
from app.schemas.worker import LivingEvaluation

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


# ── Test 1: confidence 낮을 때 재호출 (Mock) ──────────────────

async def test_confidence_retry() -> None:
    section("Test 1: Confidence ≤ 54 → Living Agent 재호출 확인 (Mock)")

    fake_accommodations = [
        {"id": "mock-001", "name": "제주 테스트 숙소", "latitude": 33.4890, "longitude": 126.4983},
    ]
    state = {**FAKE_STATE, "candidate_accommodations": fake_accommodations, "retry_count": {}}

    call_count = {"process": 0}

    async def fake_process(acc, *_):
        call_count["process"] += 1
        if call_count["process"] == 1:
            return LivingEvaluation(
                accommodation_id=acc.get("id", ""),
                score=45.0,
                confidence=30.0,  # RETRY_CONFIDENCE_THRESHOLD(54) 이하 → 재호출 트리거
                summary="첫 평가 — 정보 부족으로 신뢰도 낮음",
            )
        return LivingEvaluation(
            accommodation_id=acc.get("id", ""),
            score=72.0,
            confidence=78.0,
            summary="재호출 후 평가 — 추가 탐색으로 신뢰도 개선",
        )

    with patch("app.agents.living_agent._plan_search", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.living_agent._process", side_effect=fake_process):
        mock_plan.return_value = MagicMock()
        result = await living_agent(state)

    evals = result.get("living_evaluations", [])
    retry_count = result.get("retry_count", {})

    print(f"\n  _process 총 호출 횟수: {call_count['process']}  (기대: 2)")
    print(f"  retry_count: {retry_count}  (기대: {{'living': 1}})")
    for ev in evals:
        print(f"  숙소: {ev.accommodation_id}")
        print(f"  score: {ev.score}  confidence: {ev.confidence}")
        print(f"  summary: {ev.summary}")

    assert call_count["process"] == 2, f"재호출 미발생 (호출 횟수: {call_count['process']})"
    assert retry_count.get("living") == 1, "retry_count['living'] 가 1이 아님"
    assert evals[0].confidence == 78.0, "재호출 결과가 반영되지 않음"
    print("\n  ✅ PASS — confidence 낮을 때 재호출 정상 동작")


# ── Test 2: UserInput null 필드 → 전체 실행 확인 ─────────────

async def test_null_user_input(
    raw_accommodations: list[dict],
    accommodations: list[dict],
    selected_region: dict,
) -> None:
    section("Test 2: UserInput null 필드 포함 — Work + Local Agent 전체 실행")

    # UserInput: desired_region만 있고 나머지(transport, work_style, vibe 등)는 null
    sparse_input = UserInput(
        desired_region=FAKE_STATE["parsed_preferences"].get("desired_region"),
    )

    set_fields = [k for k, v in sparse_input.model_dump().items() if v is not None]
    none_fields = [k for k, v in sparse_input.model_dump().items() if v is None]
    print(f"\n  설정된 필드 ({len(set_fields)}개): {set_fields}")
    print(f"  None 필드  ({len(none_fields)}개): {none_fields}")

    # Planner 출력(필수 조건 3종)은 항상 보장, user_input만 sparse
    sparse_state = {
        "parsed_preferences": FAKE_STATE["parsed_preferences"],
        "must_have_conditions": FAKE_STATE["must_have_conditions"],
        "avoid_conditions": FAKE_STATE["avoid_conditions"],
        "preference_conditions": FAKE_STATE["preference_conditions"],
        "priority_weights": FAKE_STATE["priority_weights"],
        "user_input": sparse_input,
        "candidate_accommodations": raw_accommodations,
        "selected_region": selected_region,
        "retry_count": {},
        "errors": [],
    }

    # Living Agent — parsed_preferences 기반 판단, sparse user_input 영향 확인
    try:
        living_result = await living_agent(sparse_state)
        living_evals = living_result.get("living_evaluations", [])
        print(f"\n  ✅ Living Agent: {len(living_evals)}개 숙소 평가 완료")
        for ev in living_evals:
            print(f"  숙소: {ev.accommodation_id}  score: {ev.score}  confidence: {ev.confidence}")
    except Exception as e:
        print(f"\n  ❌ Living Agent 오류: {e}")

    # Work Agent — user_input에서 work_required/work_style/transport 읽음, 이 값들이 null
    try:
        work_result = work_agent(sparse_state)
        work_evals = work_result.get("work_evaluations", [])
        print(f"\n  ✅ Work Agent: {len(work_evals)}개 숙소 평가 완료")
        for ev in work_evals:
            print(f"  숙소: {ev.accommodation_id}  score: {ev.score}  confidence: {ev.confidence}")
    except Exception as e:
        print(f"\n  ❌ Work Agent 오류: {e}")

    # Local Agent — user_input에서 desired_vibe/tourism_hobby/companion 읽음, 이 값들이 null
    try:
        local_results = await evaluate_accommodations(accommodations, sparse_input)
        print(f"\n  ✅ Local Agent: {len(local_results)}개 숙소 평가 완료")
        for r in local_results:
            print(f"  숙소: {r.accommodation_id}  score: {r.score}  confidence: {r.confidence}")
    except Exception as e:
        print(f"\n  ❌ Local Agent 오류: {e}")


# ── 메인 ──────────────────────────────────────────────────────

# test_null_user_input 용 fallback (Stay Agent 숙소 검색 실패 시 사용)
_FALLBACK_RAW = [
    {"id": "fallback-001", "name": "세화 게스트하우스", "address": "제주 구좌읍 세화리",
     "mapx": "126.8676", "mapy": "33.5497", "total_score": 80, "brief_reason": "테스트용"},
    {"id": "fallback-002", "name": "세화 독채펜션", "address": "제주 구좌읍 세화4길",
     "mapx": "126.8712", "mapy": "33.5510", "total_score": 75, "brief_reason": "테스트용"},
]
_FALLBACK_NORMALIZED = [
    {"id": "fallback-001", "name": "세화 게스트하우스", "address": "제주 구좌읍 세화리",
     "latitude": 33.5497, "longitude": 126.8676, "region": "제주 구좌읍 세화리 생활권"},
    {"id": "fallback-002", "name": "세화 독채펜션", "address": "제주 구좌읍 세화4길",
     "latitude": 33.5510, "longitude": 126.8712, "region": "제주 구좌읍 세화리 생활권"},
]
_FALLBACK_REGION = {"region_name": "제주 구좌읍 세화리 생활권", "rank": 1}


async def main() -> None:
    # Test 1은 완전히 독립적 — Stay Agent 결과 불필요
    await test_confidence_retry()

    # Stay Phase 1
    candidates = await run_stay_phase1()
    selected = candidates[0]

    # Stay Phase 2
    raw_accommodations = await run_stay_phase2(selected)

    if not raw_accommodations:
        print("\n  숙소 결과 없음 — 파이프라인 스킵, Test 2는 fallback 데이터로 진행")
        await test_null_user_input(_FALLBACK_RAW, _FALLBACK_NORMALIZED, _FALLBACK_REGION)
        return

    # mapx/mapy → latitude/longitude 변환
    accommodations = _build_accommodation_list(raw_accommodations)

    if not accommodations:
        print("\n  유효 좌표 숙소 없음 — 파이프라인 스킵, Test 2는 fallback 데이터로 진행")
        await test_null_user_input(_FALLBACK_RAW, _FALLBACK_NORMALIZED, selected)
        return

    # Living → Work → Local 순차 실행
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

    # Test 2: 실제 파이프라인 데이터로
    await test_null_user_input(raw_accommodations, accommodations, selected)



# ── Test 3: planner_phase2 통합 테스트 ───────────────────────

async def test_planner_phase2(selected_region: dict) -> None:
    section("Test 3: planner_phase2 — 숙소 검색 → 정규화 → 평가 → 최종 출력")

    from app.agents.planner_agent import planner_phase2

    user_input = UserInput(
        purpose=FAKE_STATE["parsed_preferences"].get("purpose"),
        duration=FAKE_STATE["parsed_preferences"].get("duration"),
        desired_region=FAKE_STATE["parsed_preferences"].get("desired_region"),
        desired_vibe=FAKE_STATE["parsed_preferences"].get("desired_vibe"),
        tourism_hobby=FAKE_STATE["parsed_preferences"].get("tourism_hobby"),
        work_required=FAKE_STATE["parsed_preferences"].get("work_required"),
        work_style=FAKE_STATE["parsed_preferences"].get("work_style"),
        transport=FAKE_STATE["parsed_preferences"].get("transport"),
        living_infra=FAKE_STATE["parsed_preferences"].get("living_infra"),
        companion=FAKE_STATE["parsed_preferences"].get("companion"),
    )

    state = {
        **FAKE_STATE,
        "user_input":      user_input,
        "selected_region": selected_region,
        "retry_count":     {},
        "errors":          [],
    }

    result = await planner_phase2(state)

    errors = result.get("errors", [])
    if errors:
        print(f"\n  ⚠  errors: {errors}")

    # 정규화 확인
    normalized = result.get("normalized_accommodations", [])
    print(f"\n  정규화된 숙소 수: {len(normalized)}")
    for acc in normalized:
        print(f"  - {acc['name']}  lat={acc['latitude']}  lng={acc['longitude']}")

    # 각 agent 평가 확인
    for key, label in [
        ("work_evaluations",   "Work"),
        ("living_evaluations", "Living"),
        ("local_evaluations",  "Local"),
    ]:
        evals = result.get(key, [])
        print(f"\n  {label} Agent: {len(evals)}개 숙소 평가")
        for ev in evals:
            print(f"  - {ev.accommodation_id}  score={ev.score}  confidence={ev.confidence}")

    # 최종 출력 확인
    final = result.get("final_user_output")
    if final:
        print(f"\n  ✅ FinalOutput 생성 완료")
        print(f"  추천 지역: {final.recommended_region}")
        print(f"  사용자 조건 충족: {final.matched_conditions}")
        for ranked in final.ranked_accommodations:
            print(f"\n  [{ranked.rank}위] {ranked.name}  total_score={ranked.total_score}")
            print(f"  work:   {ranked.work_summary}")
            print(f"  living: {ranked.living_summary}")
            print(f"  local:  {ranked.local_summary}")
    else:
        print("\n  ❌ FinalOutput 생성 실패")


async def main() -> None:
    # Test 1은 완전히 독립적 — Stay Agent 결과 불필요
    await test_confidence_retry()

    # Stay Phase 1
    candidates = await run_stay_phase1()
    selected = candidates[0]

    # Test 3: planner_phase2 직접 호출 (accommodation_search 포함)
    await test_planner_phase2(selected)

    # Stay Phase 2 (기존 단계별 확인용)
    raw_accommodations = await run_stay_phase2(selected)

    if not raw_accommodations:
        print("\n  숙소 결과 없음 — 파이프라인 스킵, Test 2는 fallback 데이터로 진행")
        await test_null_user_input(_FALLBACK_RAW, _FALLBACK_NORMALIZED, _FALLBACK_REGION)
        return

    # mapx/mapy → latitude/longitude 변환
    accommodations = _build_accommodation_list(raw_accommodations)

    if not accommodations:
        print("\n  유효 좌표 숙소 없음 — 파이프라인 스킵, Test 2는 fallback 데이터로 진행")
        await test_null_user_input(_FALLBACK_RAW, _FALLBACK_NORMALIZED, selected)
        return

    # Living → Work → Local 순차 실행
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

    # Test 2: 실제 파이프라인 데이터로
    await test_null_user_input(raw_accommodations, accommodations, selected)


async def run_test3() -> None:
    """Test 3만 단독 실행."""
    candidates = await run_stay_phase1()
    await test_planner_phase2(candidates[0])


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    if target == "test3":
        asyncio.run(run_test3())
    else:
        asyncio.run(main())
