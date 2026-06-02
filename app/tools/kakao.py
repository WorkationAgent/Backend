from __future__ import annotations
import httpx
from typing import Literal, Optional
from pydantic import BaseModel

from app.config.settings import KAKAO_REST_API_KEY, KAKAO_LOCAL_URL


KakaoCategory = Literal[
    "AT4",  # 관광명소
    "CT1",  # 문화시설
    "FD6",  # 음식점
    "CE7",  # 카페
]


class KakaoPlace(BaseModel):
    place_id: str
    name: str
    category_name: Optional[str] = None
    category_group_code: Optional[str] = None
    address: Optional[str] = None
    road_address: Optional[str] = None
    latitude: float
    longitude: float
    distance_meters: Optional[float] = None
    place_url: Optional[str] = None
    phone: Optional[str] = None


def _to_place(doc: dict) -> KakaoPlace:
    return KakaoPlace(
        place_id=str(doc.get("id", "")),
        name=(doc.get("place_name") or "").strip(),
        category_name=doc.get("category_name"),
        category_group_code=doc.get("category_group_code"),
        address=doc.get("address_name"),
        road_address=doc.get("road_address_name"),
        latitude=float(doc.get("y") or 0),
        longitude=float(doc.get("x") or 0),
        distance_meters=float(doc["distance"]) if doc.get("distance") else None,
        place_url=doc.get("place_url"),
        phone=doc.get("phone"),
    )


async def category_search(
    latitude: float,
    longitude: float,
    radius_meters: int,
    category_group_code: KakaoCategory,
    size: int = 15,
) -> list[KakaoPlace]:
    """카테고리 코드로 반경 내 장소 검색.
    Kakao 한도: radius ≤ 20000m, size ≤ 15.
    """
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {
        "category_group_code": category_group_code,
        "x": longitude,
        "y": latitude,
        "radius": min(radius_meters, 20000),
        "size": min(size, 15),
        "sort": "distance",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{KAKAO_LOCAL_URL}/search/category.json",
            headers=headers, params=params,
        )
        r.raise_for_status()
        data = r.json()
    return [_to_place(it) for it in data.get("documents", [])]


# ── 좌표 → 행정 구역 (KTO 코드 도출용) ──────────────────────────────────
class RegionInfo(BaseModel):
    sido: str = ""       # region_1depth_name, 예: "전라북도"
    sigungu: str = ""    # region_2depth_name, 예: "남원시"
    dong: str = ""       # region_3depth_name, 예: "도촌동"


async def coord2regioncode(
    latitude: float, longitude: float
) -> Optional[RegionInfo]:
    """좌표 → 행정 구역 이름 (Kakao coord2regioncode).

    KTO areaCode/sigunguCode를 도출하기 위한 1단계.
    반환된 시도·시군구 이름을 KTO 코드 테이블에서 매칭해 코드로 변환한다.
    """
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {"x": longitude, "y": latitude}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{KAKAO_LOCAL_URL}/geo/coord2regioncode.json",
            headers=headers, params=params,
        )
        r.raise_for_status()
        data = r.json()

    docs = data.get("documents", [])
    if not docs:
        return None
    # 법정동(B) 우선, 없으면 첫 번째
    doc = next((d for d in docs if d.get("region_type") == "B"), docs[0])
    return RegionInfo(
        sido=doc.get("region_1depth_name", ""),
        sigungu=doc.get("region_2depth_name", ""),
        dong=doc.get("region_3depth_name", ""),
    )


async def keyword_search(
    query: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    radius_meters: Optional[int] = None,
    size: int = 15,
) -> list[KakaoPlace]:
    """키워드 장소 검색 – hobby/vibe 키워드로 보강."""
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params: dict = {"query": query, "size": min(size, 15), "sort": "distance"}
    if latitude is not None and longitude is not None:
        params["x"] = longitude
        params["y"] = latitude
        if radius_meters:
            params["radius"] = min(radius_meters, 20000)

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{KAKAO_LOCAL_URL}/search/keyword.json",
            headers=headers, params=params,
        )
        r.raise_for_status()
        data = r.json()
    return [_to_place(it) for it in data.get("documents", [])]
