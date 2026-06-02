from __future__ import annotations
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

class PlaceItem(BaseModel):
    name: str = ""
    dist_m: float = 0.0
    category: str = ""


class FestivalItem(BaseModel):
    name: str = ""
    period: str = ""
    dist_m: float = 0.0


class DimensionScores(BaseModel):
    matching: int = 0
    variety: int = 0
    access: int = 0
    season: int = 0


class LocalEvaluationDetails(BaseModel):
    tourism_spots: list[PlaceItem] = Field(default_factory=list)
    local_experiences: list[PlaceItem] = Field(default_factory=list)
    festivals: list[FestivalItem] = Field(default_factory=list)
    matched_hobbies: list[str] = Field(default_factory=list)
    vibe_match_note: str = ""
    dimension_scores: DimensionScores = Field(default_factory=DimensionScores)
    search_radius_used_km: float = 0.0
    data_sources: list[str] = Field(default_factory=list)
    needs_retry: bool = False






class LivingEvaluation(BaseModel):
    accommodation_id: str
    score: Optional[float] = None
    confidence: Optional[float] = None
    summary: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class LocalEvaluation(BaseModel):
    accommodation_id: str = ""
    score: float = 0.0           # was Optional[float]
    confidence: float = 0.0      # was Optional[float]
    summary: str = ""            # was Optional[str]
    details: LocalEvaluationDetails = Field(default_factory=LocalEvaluationDetails)


class WorkEvaluation(BaseModel):
    accommodation_id: str
    score: Optional[float] = None
    confidence: Optional[float] = None
    summary: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)





