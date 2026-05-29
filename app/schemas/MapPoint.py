class MapPoint(BaseModel):
    """지도에 표시될 위치 (숙소/인프라/지역경험)"""
    name: str
    category: Literal["stay", "infra", "experience"]
    latitude: float
    longitude: float
    description: Optional[str] = None