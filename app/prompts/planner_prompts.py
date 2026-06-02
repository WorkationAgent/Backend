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
  "must_have_conditions": [
    "반드시 만족해야 하는 조건 (문장 형태)"
  ],
  "avoid_conditions": [
    "피해야 할 조건 (문장 형태)"
  ],
  "preference_conditions": [
    "있으면 좋은 조건 (문장 형태)"
  ],
  "priority_weights": {{
    "work": 0.30,
    "living": 0.25,
    "transport": 0.20,
    "accommodation": 0.15,
    "local": 0.10
  }}
}}

작성 기준:
- must_have: 사용자가 우선순위 1~2위로 꼽거나 work_required=true처럼 명시적으로 필요한 것
- avoid: additional_request에서 싫다고 한 것, 동행 특성상 맞지 않는 것
- preference: 있으면 좋지만 없어도 되는 것
- priority_weights: 5개 합이 반드시 1.0, 우선순위 순서에 따라 배분
- work_required가 false거나 None이면 work 가중치를 낮게 (0.05~0.10)
"""
