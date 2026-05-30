from __future__ import annotations
from typing import Dict, Any, Optional, Literal
from pydantic import BaseModel, Field


class FinalOutput(BaseModel):
    """사용자에게 보여줄 최종 결과"""
    map_points: list[MapPoint] = Field(default_factory=list)  # 지도
    recommended_region: Optional[str] = None  # 추천 지역 및 생활권
    matched_conditions: list[str] = Field(
        default_factory=list, description="사용자 조건과 맞는 부분 (3줄 요약)"
    )
    work_environment: list[EvaluatedItem] = Field(default_factory=list)  # 작업 가능 환경
    living_elements: list[EvaluatedItem] = Field(default_factory=list)  # 생활 가능 요소
    local_experiences: list[EvaluatedItem] = Field(default_factory=list)  # 관광/로컬 경험
    accommodation_info: Optional[Dict[str, Any]] = None  # 숙소 기본 정보
    cons: Optional[str] = None  # (선택) 아쉬운 점
    distance_info: Optional[dict[str, Any]] = None  # (확장) 거리, 이동시간



class MapPoint(BaseModel):
    """지도에 표시될 위치 (숙소/인프라/지역경험)"""
    name: str
    category: Literal["stay", "infra", "experience"]
    latitude: float
    longitude: float
    description: Optional[str] = None


class EvaluatedItem(BaseModel):
    """이름, 별점, 짧은 설명 형식의 출력 항목"""
    name: str
    rating: Optional[float] = None
    description: Optional[str] = None