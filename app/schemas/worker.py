from __future__ import annotations
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

from app.schemas.local_schema import LocalEvaluationDetails


class LocalEvaluation(BaseModel):
    accommodation_id: str = ""
    score: float = 0.0
    confidence: float = 0.0
    summary: str = ""
    details: LocalEvaluationDetails = Field(default_factory=LocalEvaluationDetails)


class LivingEvaluation(BaseModel):
    accommodation_id: str
    score: Optional[float] = None
    confidence: Optional[float] = None
    summary: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class WorkEvaluation(BaseModel):
    accommodation_id: str
    score: Optional[float] = None
    confidence: Optional[float] = None
    summary: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
