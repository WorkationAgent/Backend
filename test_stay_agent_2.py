"""
Stay Agent 테스트 스크립트 2
케이스: 촌캉스, 산/시골, 자차, 커플, 고예산, 작업 없음
"""

import json
from app.agents.stay_agent import region_search_node, accommodation_search_node

fake_state = {
    "parsed_preferences": {
        "purpose": "촌캉스",
        "duration": "5일",
        "desired_region": "강원도 또는 경상도 산 근처",
        "region_style": "산, 시골, 자연",
        "desired_vibe": "힐링, 자연친화, 감성",
        "tourism_hobby": "등산, 온천, 로컬 맛집, 드라이브",
        "work_required": False,
        "work_style": None,
        "transport": "자차",
        "travel_distance": "차 30분 이내",
        "living_infra": "편의점, 마트",
        "budget": "고예산",
        "accommodation_style": "독채, 풀빌라, 오션뷰",
        "companion": "연인",
        "priority": "숙소 > 자연경관 > 관광 > 생활인프라 > 교통",
        "additional_request": "너무 사람 많은 곳은 싫고, 자연 속에서 조용하게 쉬고 싶어요. 숙소가 예쁘고 독채이면 좋겠어요.",
    },
    "must_have_conditions": [
        "독채 또는 프라이빗 숙소",
        "자연 경관 (산, 계곡, 숲)",
        "자차 이동 가능한 위치",
    ],
    "preference_conditions": [
        "온천 또는 스파",
        "등산코스 인접",
        "로컬 맛집",
        "드라이브 코스",
        "풀빌라 또는 럭셔리 독채",
    ],
    "avoid_conditions": [
        "관광객 많은 번화가",
        "도심 호텔",
        "게스트하우스",
    ],
    "priority_weights": {
        "accommodation": 0.35,
        "local": 0.25,
        "living": 0.15,
        "transport": 0.15,
        "work": 0.10,
    },
}


def test_phase1():
    print("=" * 60)
    print("Phase 1: 후보 생활권 탐색")
    print("케이스: 촌캉스 / 산·시골 / 자차 / 커플 / 고예산")
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
