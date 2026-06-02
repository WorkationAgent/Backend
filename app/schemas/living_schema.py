from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class PlacePoint(BaseModel):
    """Kakao / Naver 검색으로 수집된 장소 하나."""
    name: str
    latitude: float
    longitude: float
    distance_meters: int


class CategoryResult(BaseModel):
    """생활 인프라 카테고리(교통·식료품·의료·서비스) 하나의 검색 결과."""
    found: bool
    zone_km: Optional[float] = None       # 결과가 발견된 반경: 1.5 or 2.5 or None
    count: int = 0
    nearest_minutes: Optional[int] = None # distance_meters // 80 (도보 기준 분)
    places: List[PlacePoint] = Field(default_factory=list)
    source: Literal["both", "kakao_only", "naver_only", "none"] = "none"


class LivingDetails(BaseModel):
    """숙소 하나에 대한 4개 카테고리 결과 전체. LivingEvaluation.details 에 저장."""
    transport: CategoryResult
    grocery: CategoryResult
    medical: CategoryResult
    services: CategoryResult
    weights_applied: Dict[str, float]


class CategorySearchPlan(BaseModel):
    """LLM Planning — 카테고리 하나의 검색 전략."""
    kakao_codes: List[str]
    kakao_keywords: List[str]    # 카테고리 코드 없는 시설 (세탁소, 헬스장 등)
    naver_keywords: List[str]
    weight: float                # 0.0~1.0, 4개 합산 1.0
    priority: Literal["essential", "preferred", "optional"]


class LivingSearchPlan(BaseModel):
    """call_llm output_schema — Planning LLM이 반환하는 검색 전략 전체."""
    transport: CategorySearchPlan
    grocery: CategorySearchPlan
    medical: CategorySearchPlan
    services: CategorySearchPlan
    primary_radius_km: float     # 1차 탐색 반경 — 도보: 1.5 / 자차: LLM이 지역 맥락 반영해 결정
    retry_radius_km: float       # 재탐색 반경   — 도보: 2.5 / 자차: LLM이 결정
    reasoning: str               # LLM 판단 근거


class ReflectionResult(BaseModel):
    """call_llm output_schema — Reflection LLM이 반환하는 재탐색 판단."""
    needs_retry: bool
    retry_categories: List[str]              # 재탐색 필요한 카테고리 ["transport", "medical"]
    retry_keywords: Dict[str, List[str]]     # 카테고리별 수정된 키워드
    reasoning: str                           # 판단 근거


class LivingAssessment(BaseModel):
    """call_llm output_schema — Evaluation LLM이 반환하는 평가 결과."""
    score: float      # 0~100
    confidence: float # 0~100
    summary: str      # 2~3문장 생활 편의성 요약
