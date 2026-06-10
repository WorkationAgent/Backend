from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, TypedDict

from app.schemas.user_input import UserInput
from app.schemas.worker import WorkEvaluation, LivingEvaluation, LocalEvaluation
from app.schemas.output import FinalOutput


def _merge_dict(existing: Dict[str, int] | None, new: Dict[str, int] | None) -> Dict[str, int]:
    """retry_count 등 dict 채널 병합 리듀서. 병렬 노드의 부분 갱신을 합친다."""
    return {**(existing or {}), **(new or {})}


class GraphState(TypedDict, total=False):
    # 0. 그래프 진입 입력
    raw_user_input: str

    # 1. 사용자 원본 입력 (파싱 결과)
    user_input: UserInput
    excluded_regions: List[str]

    # 2. Planner가 해석한 조건
    parsed_preferences: Dict[str, Any]
    must_have_conditions: List[str]
    avoid_conditions: List[str]
    preference_conditions: List[str]
    priority_weights: Dict[str, float]

    # 3. Stay Agent 결과
    candidate_regions: List[Dict[str, Any]]
    selected_region: Dict[str, Any]
    candidate_accommodations: List[Dict[str, Any]]

    # 4. Sub Agent 평가 결과 (병렬 워커가 각자 다른 키에 기록)
    work_evaluations: List[WorkEvaluation]
    living_evaluations: List[LivingEvaluation]
    local_evaluations: List[LocalEvaluation]

    # 5. 동적 워커 디스패치 — 실행하지 않은 워커의 사유
    skipped_agents: Dict[str, str]

    # 6. 재호출 판단용 상태 — 병렬 워커가 동시 갱신 → 병합 리듀서
    retry_count: Annotated[Dict[str, int], _merge_dict]

    # 7. 최종 출력
    final_user_output: FinalOutput

    # 8. 오류/주의사항 — 병렬 워커가 동시 append → 연결 리듀서
    errors: Annotated[List[str], operator.add]
    warnings: Annotated[List[str], operator.add]
