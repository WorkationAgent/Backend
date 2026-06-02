from __future__ import annotations
import httpx
from typing import Optional
from pydantic import BaseModel

from app.config.settings import KTO_API_KEY, KTO_BASE_URL


# ── contentTypeId 매핑 ─────────────────────────────────────────────────────────
CONTENT_TYPE = {
    "tourist_spot": 12,    # 관광지
    "cultural":     14,    # 문화시설
    "festival":     15,    # 축제·공연·행사
    "leports":      28,    # 레포츠
    "shopping":     38,    # 쇼핑 (전통시장 포함)
    "food":         39,    # 음식점
}


# ── 데이터 모델 ────────────────────────────────────────────────────────────────
class KTOItem(BaseModel):
    content_id: str
    title: str
    addr: Optional[str] = None
    latitude: float
    longitude: float
    content_type_id: int
    image_url: Optional[str] = None
    tel: Optional[str] = None
    dist_meters: Optional[float] = None


class KTOCampingItem(BaseModel):
    """고캠핑 API 결과."""
    content_id: str
    facility_name: str
    induty: Optional[str] = None       # 캠핑장 유형 (캠핑/글램핑/카라반)
    addr: Optional[str] = None
    latitude: float
    longitude: float
    glamp_inner_fclty: Optional[str] = None  # 글램핑 내부 시설
    pet_friendly: Optional[bool] = None
    image_url: Optional[str] = None


class KTOTrail(BaseModel):
    """두루누비 코스 결과."""
    course_id: str
    course_name: str
    distance_km: Optional[float] = None
    estimated_hours: Optional[float] = None
    level: Optional[str] = None        # 코스 난이도
    start_latitude: Optional[float] = None
    start_longitude: Optional[float] = None
    description: Optional[str] = None


class CongestionInfo(BaseModel):
    """관광지 집중률 정보."""
    content_id: str
    target_date: str
    congestion_level: Optional[str] = None   # 'low' | 'medium' | 'high'
    raw_score: Optional[float] = None


# ── 공용 헬퍼 ──────────────────────────────────────────────────────────────────
async def _get(url: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _items(data: dict) -> list[dict]:
    if not isinstance(data, dict):
        return []
    items = (
        data.get("response", {})
            .get("body", {})
            .get("items") or {}
    )
    if not isinstance(items, dict):
        return []
    item = items.get("item", [])
    # KTO는 결과 1개일 때 dict로 옴
    return [item] if isinstance(item, dict) else (item if isinstance(item, list) else [])


def _to_kto_item(raw: dict, default_type: int = 12) -> KTOItem:
    # 주의: KTO는 mapy=위도, mapx=경도
    return KTOItem(
        content_id=str(raw.get("contentid", "")),
        title=(raw.get("title") or "").strip(),
        addr=raw.get("addr1") or raw.get("addr2"),
        latitude=float(raw.get("mapy") or 0),
        longitude=float(raw.get("mapx") or 0),
        content_type_id=int(raw.get("contenttypeid") or default_type),
        image_url=raw.get("firstimage") or None,
        tel=raw.get("tel") or None,
        dist_meters=float(raw["dist"]) if raw.get("dist") else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# [기본 API] locationBasedList1
# ─────────────────────────────────────────────────────────────────────────────
async def location_based_list(
    latitude: float,
    longitude: float,
    radius_meters: int,
    content_type_id: Optional[int] = None,
    num_of_rows: int = 30,
) -> list[KTOItem]:
    """위경도 기반 관광 정보 조회."""
    params = {
        "serviceKey": KTO_API_KEY,
        "mapX": longitude,
        "mapY": latitude,
        "radius": min(radius_meters, 20000),
        "MobileOS": "ETC",
        "MobileApp": "WorkationAgent",
        "_type": "json",
        "numOfRows": num_of_rows,
        "pageNo": 1,
        "arrange": "S",  # 거리순
    }
    if content_type_id is not None:
        params["contentTypeId"] = content_type_id

    data = await _get(f"{KTO_BASE_URL}/locationBasedList2", params)
    return [_to_kto_item(it) for it in _items(data)]


# ─────────────────────────────────────────────────────────────────────────────
# [기본 API] searchFestival1
# ─────────────────────────────────────────────────────────────────────────────
async def search_festival(
    latitude: float,
    longitude: float,
    radius_meters: int,
    start_date: str,            # "YYYYMMDD"
    end_date: Optional[str] = None,
    num_of_rows: int = 30,
) -> list[KTOItem]:
    """축제·공연 검색."""
    params = {
        "serviceKey": KTO_API_KEY,
        "mapX": longitude,
        "mapY": latitude,
        "radius": min(radius_meters, 20000),
        "MobileOS": "ETC",
        "MobileApp": "WorkationAgent",
        "_type": "json",
        "numOfRows": num_of_rows,
        "pageNo": 1,
        "arrange": "S",
        "eventStartDate": start_date,
    }
    if end_date:
        params["eventEndDate"] = end_date

    data = await _get(f"{KTO_BASE_URL}/searchFestival2", params)
    return [_to_kto_item(it, default_type=15) for it in _items(data)]


# ─────────────────────────────────────────────────────────────────────────────
# [기본 API] areaBasedList2 — 시군구 단위 지역 대표 명소
# ─────────────────────────────────────────────────────────────────────────────
async def area_based_list(
    area_code: str,
    sigungu_code: Optional[str] = None,
    content_type_id: Optional[int] = None,
    num_of_rows: int = 20,
    arrange: str = "P",   # P=인기순(대표성), O=제목순, Q=수정일순
) -> list[KTOItem]:
    """areaBasedList2 — 시군구 단위 대표 관광지 (KTO KorService2).

    arrange='P'(인기순)으로 받으면 그 지역의 '대표 명소'에 가까운 순위가 됨.
    """
    params = {
        "serviceKey": KTO_API_KEY,
        "areaCode": area_code,
        "MobileOS": "ETC",
        "MobileApp": "WorkationAgent",
        "_type": "json",
        "numOfRows": num_of_rows,
        "pageNo": 1,
        "arrange": arrange,
    }
    if sigungu_code:
        params["sigunguCode"] = sigungu_code
    if content_type_id is not None:
        params["contentTypeId"] = content_type_id

    data = await _get(f"{KTO_BASE_URL}/areaBasedList2", params)
    return [_to_kto_item(it) for it in _items(data)]


# ─────────────────────────────────────────────────────────────────────────────
# [숙소 검색] Stay Agent 전용
# ─────────────────────────────────────────────────────────────────────────────
async def search_accommodations(region_name: str, max_rows: int = 20) -> list[dict]:
    """생활권명 기반 숙박 시설 검색.

    시/도 단위부터 순서대로 시도한다.
    """
    cleaned = region_name.replace("생활권", "").strip()
    keywords = cleaned.split()[:2]
    for keyword in keywords:
        result = await _search_accommodation_by_keyword(keyword, max_rows)
        if result:
            return result
    return []


async def _search_accommodation_by_keyword(keyword: str, max_rows: int) -> list[dict]:
    params = {
        "serviceKey": KTO_API_KEY,
        "numOfRows": max_rows,
        "pageNo": 1,
        "MobileOS": "ETC",
        "MobileApp": "WorkationPlanner",
        "_type": "json",
        "contentTypeId": 32,
        "keyword": keyword,
    }
    try:
        data = await _get(f"{KTO_BASE_URL}/searchKeyword2", params)
        items = data.get("response", {}).get("body", {}).get("items", {})
        if not items:
            return []
        raw = items.get("item", [])
        return raw if isinstance(raw, list) else [raw]
    except Exception:
        return []


def simplify_accommodation(item: dict) -> dict:
    """LLM에 넘길 숙소 정보를 최소화한다."""
    return {
        "id": str(item.get("contentid", "")),
        "name": item.get("title", ""),
        "address": f"{item.get('addr1', '')} {item.get('addr2', '')}".strip(),
        "category": item.get("cat3", ""),
        "image_url": item.get("firstimage") or None,
        "homepage": item.get("homepage") or None,
        "tel": item.get("tel") or None,
        "mapx": item.get("mapx") or None,
        "mapy": item.get("mapy") or None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# [확장 API – 스텁] 고캠핑
# ─────────────────────────────────────────────────────────────────────────────
async def gocamping_location(
    latitude: float,
    longitude: float,
    radius_meters: int,
    num_of_rows: int = 20,
) -> list[KTOCampingItem]:
    """고캠핑 위치 기반 – 캠핑장/글램핑/카라반.

    TODO: 공식 문서로 엔드포인트/파라미터 확인 후 구현.
    https://www.data.go.kr/data/15101933/openapi.do
    """
    raise NotImplementedError("고캠핑 API 엔드포인트 확인 후 구현")


# ─────────────────────────────────────────────────────────────────────────────
# [확장 API – 스텁] 두루누비
# ─────────────────────────────────────────────────────────────────────────────
async def durunubi_courses(
    latitude: float,
    longitude: float,
    radius_meters: int,
    num_of_rows: int = 20,
) -> list[KTOTrail]:
    """두루누비 – 걷기길/자전거길 코스.

    TODO: 공식 문서로 엔드포인트/파라미터 확인 후 구현.
    """
    raise NotImplementedError("두루누비 API 엔드포인트 확인 후 구현")


# ─────────────────────────────────────────────────────────────────────────────
# [확장 API – 스텁] 관광지 연관 관광지
# ─────────────────────────────────────────────────────────────────────────────
async def related_attractions(content_id: str) -> list[KTOItem]:
    """특정 관광지와 연관된 다른 관광지들.

    TODO: 공식 문서로 엔드포인트 확인 후 구현.
    """
    raise NotImplementedError("연관 관광지 API 엔드포인트 확인 후 구현")


# ─────────────────────────────────────────────────────────────────────────────
# [확장 API – 스텁] 관광지 집중률
# ─────────────────────────────────────────────────────────────────────────────
async def attraction_congestion(
    content_id: str,
    target_date: str,
) -> Optional[CongestionInfo]:
    """관광지 집중률 (혼잡도).

    TODO: 공식 문서로 엔드포인트/파라미터 확인 후 구현.
    """
    raise NotImplementedError("관광지 집중률 API 엔드포인트 확인 후 구현")
