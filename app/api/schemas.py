from typing import Any, Optional
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
    match_reasons: list[str]
    weaknesses: list[str]
    is_best: bool = False


class PlanResponse(BaseModel):
    thread_id: str
    parsed: ParsedConditions
    candidate_regions: list[RegionCandidate]


# ── Phase 2 ──────────────────────────────────────────────────────────

class SelectRegionRequest(BaseModel):
    thread_id: str
    region_id: str


class EvaluatedItem(BaseModel):
    name: str
    rating: Optional[float] = None
    description: Optional[str] = None
    distance_text: Optional[str] = None


class MapPoint(BaseModel):
    name: str
    category: str
    latitude: float
    longitude: float
    description: Optional[str] = None


class CategoryScores(BaseModel):
    work: Optional[float] = None
    living: Optional[float] = None
    local: Optional[float] = None


class AccommodationResult(BaseModel):
    rank: int
    overall_score: float
    category_scores: CategoryScores
    name: str
    accommodation_id: str
    location_text: Optional[str] = None
    map_points: list[MapPoint] = []
    matched_conditions: list[str] = []
    work_summary: Optional[str] = None
    living_summary: Optional[str] = None
    local_summary: Optional[str] = None
    work_environment: list[EvaluatedItem] = []
    living_elements: list[EvaluatedItem] = []
    local_experiences: list[EvaluatedItem] = []


class RecommendResponse(BaseModel):
    recommended_region: str
    candidates: list[AccommodationResult]
