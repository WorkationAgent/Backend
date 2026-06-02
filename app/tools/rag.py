"""지역 특색 RAG 검색.

v1에서는 빈 리스트를 반환하는 stub. 평가 흐름을 정의함.
v2에서 Chroma 인덱스 구축 후 본체 구현 예정.

인덱싱 대상: 지자체 관광 페이지·여행 칼럼·위키 등 풍부한 지역 특색 텍스트.
청크 단위: 지역 × 테마 (예: "강릉 - 커피거리", "전남 보성 - 차밭").
메타데이터: region, themes, vibe_tags, season_peak.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel


class RegionalContextChunk(BaseModel):
    """RAG로 검색된 지역 특색 청크."""
    region: str
    text: str
    themes: list[str] = []
    vibe_tags: list[str] = []
    score: Optional[float] = None  # 유사도 점수


async def retrieve_regional_context(
    region: str,
    user_hints: list[str],   # [tourism_hobby, desired_vibe, region_style] 등
    top_k: int = 5,
) -> list[RegionalContextChunk]:
    """지역 특색 RAG 검색.

    v1: stub (빈 리스트).
    v2 구현 시:
      1. region 메타필터 적용
      2. " ".join(user_hints)로 시맨틱 검색
      3. top_k 청크 반환
    """
    # TODO v2: Chroma 클라이언트 연결 + 임베딩 검색
    return []
