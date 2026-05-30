from __future__ import annotations
from typing import TypedDict, Dict, Any, List

from app.schemas.user_input import UserInput
from app.schemas.worker import WorkEvaluation, LivingEvaluation, LocalEvaluation
from app.schemas.output import FinalOutput


class GraphState(TypedDict):
    # 1. 사용자 원본 입력
    user_input: UserInput

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

    # 4. Sub Agent 평가 결과
    work_evaluations: List[WorkEvaluation]
    living_evaluations: List[LivingEvaluation]
    local_evaluations: List[LocalEvaluation]

    # 5. Planner 통합 결과
    integrated_scores: List[Dict[str, Any]]
    ranked_recommendations: List[Dict[str, Any]]

    # 6. 재호출 판단용 상태
    evaluation_status: Dict[str, str]
    missing_information: List[Dict[str, Any]]
    retry_count: Dict[str, int]
    # retry_history: List[Dict[str, Any]]
    # retry_requests: List[Dict[str, Any]]

    # 7. 최종 출력
    final_user_output: FinalOutput
    # final_eval_output: Dict[str, Any]

    # 8. 오류/주의사항
    errors: List[str]
    warnings: List[str]