from typing import Any, Optional, Literal
from pydantic import BaseModel


# ── Phase 1 ──────────────────────────────────────────────────────────

class PlanRequest(BaseModel):
    text: str


class ParsedConditions(BaseModel):
    must_have: list[str]
    preferences: list[str]


class RegionCandidate(BaseModel):
    id: str
    name: str
    living_area: str
    description: str
    tags: list[str]
    match_summary: str
    weaknesses: list[str]
    is_best: bool = False
    photo_url: Optional[str] = None


class PlanResponse(BaseModel):
    thread_id: str
    parsed: ParsedConditions
    candidate_regions: list[RegionCandidate]


# ── Phase 2 ──────────────────────────────────────────────────────────

class SelectRegionRequest(BaseModel):
    thread_id: str
    region_id: str


MapPointKind = Literal["stay", "work", "living", "local"]


class MapPoint(BaseModel):
    name: str
    kind: MapPointKind
    lat: float
    lng: float
    description: Optional[str] = None


class EvaluatedItem(BaseModel):
    name: str
    sub: str            # 짧은 설명 / 거리
    rating: float = 0.0


class EvaluationSection(BaseModel):
    score: float = 0.0
    summary: str = ""
    items: list[EvaluatedItem] = []


class CategoryScores(BaseModel):
    work: float = 0.0
    living: float = 0.0
    local: float = 0.0


class AccommodationResult(BaseModel):
    rank: int
    overall_score: float
    name: str
    address: str
    center: dict[str, float]    # {"lat": ..., "lng": ...}
    map_points: list[MapPoint] = []
    category_scores: CategoryScores
    sections: dict[str, EvaluationSection]   # work / living / local


class RecommendResponse(BaseModel):
    recommended_region: str
    results_subtitle: str
    candidates: list[AccommodationResult]
