class LivingEvaluation(BaseModel):
    accommodation_id: str
    score: Optional[float] = None
    summary: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)