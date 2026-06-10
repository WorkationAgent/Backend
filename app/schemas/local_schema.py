from __future__ import annotations
from pydantic import BaseModel, Field

class PlaceItem(BaseModel):
    """장소 한 건 (시그니처 명소 / 일상 거리 공용)."""
    name: str = ""
    dist_m: float = 0.0
    category: str = ""
    is_signature: bool = False    # 지역 대표 명소 여부 (KTO areaBased/RAG 출처)
    latitude: float | None = None   # 지도 핀용 좌표 (코드가 원본 장소에서 매칭해 채움)
    longitude: float | None = None


class DimensionScores(BaseModel):
    """차원별 실제 점수 (합 = LocalEvaluation.score).
    각 값의 의미는 dimension_weights에 따라 달라짐.
    """
    signature: float = 0   # 지역 시그니처 명소·특별 경험의 풍부함·접근성
    access: float = 0      # 시그니처들이 숙소에서 닿는 정도
    daily: float = 0       # 체류 중 매일 들를 카페·산책·맛집
    fit: float = 0         # 사용자 hobby/vibe 매칭


class DimensionWeights(BaseModel):
    """이번 평가에서 LLM이 사용한 가중치 (합 = 100).
    purpose 라벨이 아니라 user_input의 신호(work_required·duration·
    transport·hobby 등)를 종합해 LLM이 동적으로 결정.
    기본값(신호 희미할 때): signature 35 / access 25 / daily 25 / fit 15
    """
    signature: int = 35
    access: int = 25
    daily: int = 25
    fit: int = 15


class LocalEvaluationDetails(BaseModel):
    """Local Agent의 details — 개선 타입.

    list[dict]·Dict[str, Any] 같은 generic 컬렉션은 LLM이 빈 dict로
    채우는 경향이 있어, 중첩까지 typed로 개선함.
    """
    signature_spots: list[PlaceItem] = Field(default_factory=list)   # 지역 대표 명소 (최대 5)
    daily_spots: list[PlaceItem] = Field(default_factory=list)       # 매일 들를 거리 (최대 5)
    matched_hobbies: list[str] = Field(default_factory=list)
    vibe_match_note: str = ""
    dimension_scores: DimensionScores = Field(default_factory=DimensionScores)
    dimension_weights: DimensionWeights = Field(default_factory=DimensionWeights)
    weight_rationale: str = ""        # 왜 이렇게 가중치했는지 한 줄
    search_radius_used_km: float = 0.0
    data_sources: list[str] = Field(default_factory=list)
    needs_retry: bool = False
