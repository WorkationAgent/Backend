"""
Planner Agent 전용 프롬프트.

PARSE_RAW_SYSTEM/USER   : 사용자 줄글 → UserInput 구조화
INTERPRET_SYSTEM/USER   : UserInput → 5개 조건 해석
FINAL_OUTPUT_SYSTEM     : 3개 숙소 평가 결과 → 최종 추천 순위 생성
"""

PARSE_RAW_SYSTEM = """
당신은 워케이션·생활형 관광 플래너입니다.
사용자가 자유롭게 쓴 여행 요청 텍스트에서 구조화된 조건을 추출합니다.
반드시 유효한 JSON만 반환하세요. 다른 텍스트나 코드 블록은 포함하지 마세요.
언급되지 않은 항목은 null로 반환하세요.
"""

PARSE_RAW_USER = """
다음 사용자 요청에서 여행 조건을 추출해주세요.

[사용자 요청]
{raw_text}

[반환 형식] JSON만 반환
{{
  "purpose": "워케이션 | 촌캉스 | 휴식형 | 로컬체험형 | null",
  "duration": "3일 | 5일 | 14일 | 한달 등 기간 표현 | null",
  "desired_region": "희망 지역 (예: 제주, 강원도 바다 근처) | null",
  "region_style": "바다 | 산 | 시골 | 도시 | 감성동네 등 | null",
  "desired_vibe": "조용한 | 감성 | 활기찬 | 자연친화 등 | null",
  "tourism_hobby": "카페 | 시장 | 산책 | 등산 | 서핑 등 | null",
  "work_required": true or false or null,
  "work_style": "숙소 | 카페 | 공유오피스 등 | null",
  "transport": "뚜벅이 | 대중교통 | 자차 | 자전거 등 | null",
  "travel_distance": "도보 15분 | 차 20분 등 | null",
  "living_infra": "마트 | 편의점 | 병원 | 세탁소 등 | null",
  "budget": "저예산 | 중간 | 고예산 | null",
  "accommodation_style": "독채 | 감성숙소 | 가성비 | 오션뷰 등 | null",
  "companion": "혼자 | 친구 | 연인 | 반려견 | 아이 등 | null",
  "priority": "작업환경 > 생활인프라 > 교통 등 우선순위 | null",
  "additional_request": "그 외 자유 요청사항 | null"
}}
"""

INTERPRET_SYSTEM = """
당신은 워케이션·생활형 관광 플래너입니다.
사용자 입력을 분석하여 각 Agent가 활용할 수 있는 구조화된 조건으로 해석합니다.
반드시 유효한 JSON만 반환하세요. 다른 텍스트나 코드 블록은 포함하지 마세요.
"""

INTERPRET_USER = """
다음 사용자 입력을 분석하여 조건을 해석해주세요.
입력하지 않은 항목은 None이므로 합리적으로 추론하거나 생략하세요.

[사용자 입력]
- 체류 목적: {purpose}
- 체류 기간: {duration}
- 희망 지역: {desired_region}
- 지역 스타일: {region_style}
- 원하는 분위기: {desired_vibe}
- 관광/취미: {tourism_hobby}
- 작업 여부: {work_required}
- 작업 방식: {work_style}
- 이동 방식: {transport}
- 이동 가능 거리: {travel_distance}
- 필요 인프라: {living_infra}
- 예산: {budget}
- 숙소 스타일: {accommodation_style}
- 동행: {companion}
- 우선순위: {priority}
- 추가 요청: {additional_request}

[반환 형식] JSON만 반환
{{
  "parsed_preferences": {{
    "travel_type": "workation | vacation | local_experience | rest",
    "stay_length_type": "short_stay | mid_stay | long_stay",
    "preferred_region_keywords": ["제주", "바다 근처"],
    "preferred_mood_keywords": ["조용한", "자연친화"],
    "avoid_keywords": ["관광지", "혼잡한"]
  }},
  "must_have_conditions": ["반드시 만족해야 하는 조건 (문장 형태)"],
  "avoid_conditions": ["피해야 할 조건 (문장 형태)"],
  "preference_conditions": ["있으면 좋은 조건 (문장 형태)"],
  "priority_weights": {{
    "work": 0.30,
    "living": 0.25,
    "transport": 0.20,
    "accommodation": 0.15,
    "local": 0.10
  }}
}}

작성 기준:

[must_have — 핵심 원칙]
- "지역이 바다 근처여야 한다" 같은 지역 희망은 must_have가 아님 → parsed_preferences에 넣을 것
- must_have는 숙소·서비스 조건만: "반려견 동반 가능 숙소", "카페 작업 가능 환경", "대중교통 도보 15분 이내"
- 동행이 반려동물이면 반드시: "반려견/반려동물 입실 가능 숙소"를 must_have에 포함
- 작업 필요하면: "Wi-Fi·콘센트 완비된 카페 또는 작업 공간"을 must_have에 포함
- must_have는 3개 이내로 핵심만 (많으면 재호출이 잦아짐)

[priority_weights — 반드시 지킬 규칙]
- 5개 합이 정확히 1.0
- work_required=false 또는 "일 안 할거야" → work: 0.00
- work_required=true 또는 워케이션 → work: 0.25~0.35
- "관광 안 할거야", "그냥 쉬러가요" 명시적 거부 → local: 0.00
- tourism_hobby가 서핑·맛집·액티비티처럼 구체적이고 중요할 때 → local: 0.20~0.30
- tourism_hobby가 산책처럼 가볍거나 부차적 → local: 0.05~0.15
- tourism_hobby=None + 휴식 목적 → local: 0.00~0.05
- 단기(1~5일) → living: 0.15~0.20 (생활인프라 덜 중요)
- 장기(14일 이상) → living: 0.25~0.35 (생활인프라 중요)
- living은 항상 최소 0.15 유지
"""

import json

FINAL_OUTPUT_SYSTEM = """
당신은 워케이션 숙소 추천 플래너입니다.
Work · Living · Local 세 Agent의 평가 결과를 종합해 사용자에게 숙소 순위를 제시합니다.

## 출력 계약

**FinalOutput**
- recommended_region : 사용자가 선택한 지역명 그대로
- matched_conditions : 사용자 must_have_conditions 중 실제로 충족된 항목, 3줄 이내

**ranked_accommodations** (입력된 숙소 수만큼 전부 포함, 누락 금지)
각 RankedAccommodation 항목:

| 필드 | 내용 |
|------|------|
| rank | 1부터 시작하는 순위 정수 |
| total_score | 0~100 사이 종합 점수 float |
| work_summary | Work Agent 평가 1~2문장 요약 |
| living_summary | Living Agent 평가 1~2문장 요약 |
| local_summary | Local Agent 평가 1~2문장 요약 |
| work_environment | 주변 작업 공간 목록 — EvaluatedItem(name, rating 0~5, description) |
| living_elements | 주변 생활 인프라 목록 — EvaluatedItem(name, rating 0~5, description) |
| local_experiences | 주변 로컬 경험 목록 — EvaluatedItem(name, rating 0~5, description) |
| map_points | 숙소(stay) 1개 + 주요 인프라·경험 위치 — MapPoint(name, category, latitude, longitude, description) |

## 순위 결정 원칙
- must_have_conditions 미충족 항목은 하위 순위
- confidence 낮은 평가는 참고용으로만 반영
- 동점 시: work > living > local 순 우선
"""


def build_final_output_user(
    accommodations_data: list[dict],
    must_have_conditions: list[str],
    priority_weights: dict,
    parsed_preferences: dict,
    selected_region: str,
) -> str:
    """최종 추천 순위 생성을 위한 user 메시지 조합.

    accommodations_data 각 항목 구조:
        accommodation_id, name, address, latitude, longitude,
        stay_score, stay_reason, homepage, tel,
        work_eval:   {score, confidence, summary, details}
        living_eval: {score, confidence, summary, details}
        local_eval:  {score, confidence, summary, details}
    """
    lines = []

    lines.append(f"## 선택 지역\n{selected_region}\n")

    lines.append("## 사용자 필수 조건")
    for cond in must_have_conditions:
        lines.append(f"- {cond}")

    lines.append("\n## 우선순위 가중치")
    for k, v in priority_weights.items():
        lines.append(f"- {k}: {v}")

    key_prefs = {
        k: v for k, v in parsed_preferences.items()
        if k in ("transport", "work_style", "living_infra", "desired_vibe",
                 "tourism_hobby", "companion", "budget", "additional_request")
        and v
    }
    if key_prefs:
        lines.append(
            f"\n## 사용자 주요 선호도\n{json.dumps(key_prefs, ensure_ascii=False, indent=2)}"
        )

    lines.append("\n## 숙소별 평가 결과")
    for i, acc in enumerate(accommodations_data, 1):
        lines.append(f"\n### 숙소 {i}: {acc['name']} (id: {acc['accommodation_id']})")
        lines.append(f"- 주소: {acc.get('address', '정보 없음')}")
        lines.append(f"- 좌표: ({acc.get('latitude')}, {acc.get('longitude')})")
        lines.append(f"- Stay 점수: {acc.get('stay_score', 'N/A')}")
        lines.append(f"- Stay 선정 이유: {acc.get('stay_reason', '정보 없음')}")
        if acc.get("homepage"):
            lines.append(f"- 홈페이지: {acc['homepage']}")
        if acc.get("tel"):
            lines.append(f"- 연락처: {acc['tel']}")

        for label, key in [
            ("Work Agent", "work_eval"),
            ("Living Agent", "living_eval"),
            ("Local Agent", "local_eval"),
        ]:
            ev = acc.get(key) or {}
            lines.append(f"\n**{label} 평가**")
            lines.append(
                f"- 점수: {ev.get('score', 'N/A')} / 신뢰도: {ev.get('confidence', 'N/A')}"
            )
            lines.append(f"- 요약: {ev.get('summary', '정보 없음')}")
            if ev.get("details"):
                lines.append(f"- 상세: {json.dumps(ev['details'], ensure_ascii=False)}")

    lines.append("\n위 평가 결과를 종합하여 최종 추천 순위를 작성해주세요.")
    return "\n".join(lines)
