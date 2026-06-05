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
    sub: str                              # 짧은 설명
    distance_text: Optional[str] = None   # 숙소 기준 이동거리/시간 ("도보 5분", "차 8분")


class EvaluationSection(BaseModel):
    score: float = 0.0
    summary: str = ""
    items: list[EvaluatedItem] = []


class CategoryScores(BaseModel):
    work: float = 0.0
    living: float = 0.0
    local: float = 0.0


class AccommodationInfo(BaseModel):
    price: Optional[str] = None
    phone: Optional[str] = None
    homepage: Optional[str] = None


class AccommodationResult(BaseModel):
    rank: int
    overall_score: float
    name: str
    address: str
    center: dict[str, float]    # {"lat": ..., "lng": ...}
    search_radius_m: Optional[float] = None   # 세 에이전트 중 최대 검색 반경(m)
    matched_conditions: list[str] = []        # 이 숙소가 충족하는 사용자 조건
    map_points: list[MapPoint] = []
    category_scores: CategoryScores
    sections: dict[str, EvaluationSection]   # work / living / local
    accommodation_info: Optional[AccommodationInfo] = None   # 가격/연락처/홈페이지


class RecommendResponse(BaseModel):
    recommended_region: str
    results_subtitle: str
    matched_conditions: list[str] = []   # 사용자 조건 중 충족된 항목 (✓ 표시용)
    candidates: list[AccommodationResult]
