"""
Planner Agent 전용 프롬프트.

FINAL_OUTPUT_SYSTEM : 3개 숙소 평가 결과 → 최종 추천 순위 생성
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
