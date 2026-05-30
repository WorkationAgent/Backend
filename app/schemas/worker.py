from __future__ import annotations
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field


class LivingEvaluation(BaseModel):
    accommodation_id: str
    score: Optional[float] = None
    summary: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class LocalEvaluation(BaseModel):
    accommodation_id: str
    score: Optional[float] = None
    summary: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class WorkEvaluation(BaseModel):
    accommodation_id: str
    score: Optional[float] = None
    summary: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)