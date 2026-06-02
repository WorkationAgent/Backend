"""
Stay Agent 테스트 스크립트
Planner 없이 가짜 state 데이터로 Phase 1, 2 순서대로 실행
"""

import json
from app.agents.stay_agent import region_search_node, accommodation_search_node

# ── 가짜 Planner 출력 데이터 ───────────────────────────────────
fake_state = {
    "parsed_preferences": {
        "purpose": "워케이션",
        "duration": "14일",
        "desired_region": "제주 또는 바다 근처",
        "region_style": "바다, 감성동네",
        "desired_vibe": "조용한, 자연친화",
        "tourism_hobby": "카페, 산책, 맛집탐방",
        "work_required": True,
        "work_style": "카페 작업, 숙소 작업",
        "transport": "뚜벅이, 대중교통",
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
        "바다 근처",
        "조용한 분위기",
        "카페 접근성",
        "산책 가능한 환경",
        "감성숙소",
    ],
    "avoid_conditions": [
        "너무 관광지화된 지역",
        "혼잡한 지역",
    ],
    "priority_weights": {
        "work": 0.30,
        "living": 0.25,
        "transport": 0.20,
        "accommodation": 0.15,
        "local": 0.10,
    },
}


def test_phase1():
    print("=" * 60)
    print("Phase 1: 후보 생활권 탐색")
    print("=" * 60)

    result = region_search_node(fake_state)
    candidates = result["candidate_regions"]

    for c in candidates:
        print(f"\n[{c['rank']}순위] {c['region_name']}")
        print(f"  적합도: {c['initial_fit_score']}점")
        print(f"  특징: {', '.join(c['characteristics'])}")
        print(f"  이유: {c['brief_reason']}")
        print(f"  리스크: {', '.join(c['possible_risks'])}")

    return candidates


def test_phase2(candidates):
    print("\n" + "=" * 60)
    print("Phase 2: 숙소 탐색 & 점수화")
    print("사용자가 1순위 지역 선택")
    print("=" * 60)

    selected = candidates[0]
    print(f"\n선택된 지역: {selected['region_name']}")

    state_with_selection = {
        **fake_state,
        "selected_region": selected,
    }

    result = accommodation_search_node(state_with_selection)
    accommodations = result.get("candidate_accommodations", [])
    warnings = result.get("warnings", [])

    if warnings:
        print(f"\n경고: {warnings}")

    for a in accommodations:
        print(f"\n[{a['rank']}위] {a['name']}")
        print(f"  주소: {a.get('address', '')}")
        print(f"  총점: {a['total_score']}점")
        print(f"  점수 세부: {json.dumps(a.get('score_breakdown', {}), ensure_ascii=False)}")
        print(f"  이유: {a.get('brief_reason', '')}")
        print(f"  좌표: ({a.get('mapy')}, {a.get('mapx')})")


if __name__ == "__main__":
    candidates = test_phase1()
    test_phase2(candidates)
