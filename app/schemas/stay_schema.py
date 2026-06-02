from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class RegionCandidate(BaseModel):
    region_id: str
    region_name: str
    rank: int
    area_type: List[str] = Field(default_factory=list)
    brief_reason: str
    characteristics: List[str] = Field(default_factory=list)
    mood: Optional[str] = None
    initial_fit_score: int
    possible_risks: List[str] = Field(default_factory=list)


class Accommodation(BaseModel):
    id: str
    rank: int
    name: str
    address: Optional[str] = None
    total_score: float
    score_breakdown: Dict[str, float] = Field(default_factory=dict)
    brief_reason: Optional[str] = None
    image_url: Optional[str] = None
    homepage: Optional[str] = None
    tel: Optional[str] = None
    mapx: Optional[str] = None
    mapy: Optional[str] = None
