from __future__ import annotations
from typing import Optional, Literal
from pydantic import BaseModel, Field


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


class RankedAccommodation(BaseModel):
    """순위별 숙소 1개의 평가 결과"""
    rank: int
    accommodation_id: str
    name: str
    address: Optional[str] = None
    total_score: float
    work_summary: Optional[str] = None
    living_summary: Optional[str] = None
    local_summary: Optional[str] = None
    work_environment: list[EvaluatedItem] = Field(default_factory=list)
    living_elements: list[EvaluatedItem] = Field(default_factory=list)
    local_experiences: list[EvaluatedItem] = Field(default_factory=list)
    map_points: list[MapPoint] = Field(default_factory=list)
    image_url: Optional[str] = None
    homepage: Optional[str] = None
    tel: Optional[str] = None


class FinalOutput(BaseModel):
    """사용자에게 보여줄 최종 결과"""
    recommended_region: Optional[str] = None
    matched_conditions: list[str] = Field(
        default_factory=list, description="사용자 조건과 맞는 부분 (3줄 요약)"
    )
    ranked_accommodations: list[RankedAccommodation] = Field(default_factory=list)
