class EvaluatedItem(BaseModel):
    """이름, 별점, 짧은 설명 형식의 출력 항목"""
    name: str
    rating: Optional[float] = None
    description: Optional[str] = None