"""
place_tool.py — 카카오 로컬 API 기반 작업 공간 검색 도구

Work Agent가 숙소 주변의 카페·공유오피스·스터디카페·도서관을
검색하고 이동수단(도보/자차)에 맞는 접근성을 계산하는 데 사용한다.
"""

from app.config.settings import (
    KAKAO_LOCAL_URL,
    KAKAO_REST_API_KEY,
    SEARCH_RADIUS_CAR_KM,
    SEARCH_RADIUS_CAR_SPEED_KMH,
    SEARCH_RADIUS_WALK_KM,
)
from app.tools.distance_util import calc_distance_km, calc_walk_minutes

# 장소 유형별 편의시설 추정값
# (wifi, outlet, long_stay, quiet, large_table, pet_friendly, craft_allowed, parking)
_AMENITY_BY_TYPE: dict[str, tuple] = {
    "cafe":       (True,  True,  True,  None,  False, False, False, False),
    "coworking":  (True,  True,  True,  True,  True,  False, True,  True),
    "study_cafe": (True,  True,  True,  True,  True,  False, False, False),
    "library":    (True,  True,  True,  True,  True,  False, False, True),
}

_KEYWORD_TO_TYPE: dict[str, str] = {
    "카페":      "cafe",
    "공유오피스": "coworking",
    "스터디카페": "study_cafe",
    "도서관":    "library",
}

_DUMMY_TEMPLATES: list[tuple] = [
    ("감성 카페 A",   "cafe",       0.002,  0.001, True,  True,  True,  True,  False, False, False, False),
    ("공유오피스 B",  "coworking",  0.004,  0.003, True,  True,  True,  True,  True,  False, True,  True),
    ("스터디카페 C",  "study_cafe", 0.003, -0.002, True,  True,  True,  True,  True,  False, False, False),
    ("시립 도서관 D", "library",   -0.005,  0.004, True,  True,  True,  True,  True,  False, False, True),
    ("로컬 카페 E",   "cafe",      -0.001, -0.003, True,  False, False, False, False, True,  False, False),
]


def is_car_transport(transport: str | None) -> bool:
    """사용자 이동수단이 자차 기반인지 판단한다."""
    if not transport:
        return False
    return any(kw in transport for kw in ["자차", "자동차", "차량", "운전", "차로"])


def search_workplaces(
    mapx: float,
    mapy: float,
    transport: str | None = None,
    radius_km: float | None = None,
) -> list[dict]:
    """카카오 로컬 API로 숙소 주변 작업 가능 장소를 검색한다.

    이동수단에 따라 검색 반경과 이동 시간 계산 방식을 분기한다.
    - 도보/뚜벅이: SEARCH_RADIUS_WALK_KM 반경, 도보 시간
    - 자차: SEARCH_RADIUS_CAR_KM 반경, 운전 시간 (SEARCH_RADIUS_CAR_SPEED_KMH 기준)

    Args:
        mapx:      숙소 경도 (KTO mapx)
        mapy:      숙소 위도 (KTO mapy)
        transport: 사용자 이동수단 문자열 (예: "도보 위주 뚜벅이", "자차")
        radius_km: 검색 반경(km). None이면 이동수단 기준으로 자동 설정.

    Returns:
        작업 가능 장소 목록 (이동 시간 오름차순, 최대 5개).
        API 실패 시 좌표 기반 더미 데이터를 반환한다.
    """
    by_car = is_car_transport(transport)
    if radius_km is None:
        radius_km = SEARCH_RADIUS_CAR_KM if by_car else SEARCH_RADIUS_WALK_KM

    try:
        return _search_via_kakao(mapx, mapy, radius_km, by_car)
    except Exception:
        return _search_dummy(mapx, mapy, radius_km)


def _search_via_kakao(
    mapx: float,
    mapy: float,
    radius_km: float,
    by_car: bool,
) -> list[dict]:
    """카카오 로컬 키워드 검색 API를 호출해 작업 공간 목록을 반환한다."""
    import httpx

    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    workplaces: list[dict] = []
    seen_ids: set[str] = set()

    for keyword, ptype in _KEYWORD_TO_TYPE.items():
        params = {
            "query":  keyword,
            "x":      mapx,
            "y":      mapy,
            "radius": min(int(radius_km * 1000), 20000),  # 카카오 최대 20km
            "size":   5,
            "sort":   "distance",
        }
        resp = httpx.get(
            f"{KAKAO_LOCAL_URL}/search/keyword.json",
            headers=headers,
            params=params,
            timeout=5,
        )
        resp.raise_for_status()

        for doc in resp.json().get("documents", []):
            place_id = str(doc.get("id", ""))
            if place_id in seen_ids:
                continue

            place_lon = _to_float(doc.get("x"))
            place_lat = _to_float(doc.get("y"))
            if place_lat is None or place_lon is None:
                continue

            dist_km = calc_distance_km(mapy, mapx, place_lat, place_lon)
            if by_car:
                travel_min = round(dist_km / SEARCH_RADIUS_CAR_SPEED_KMH * 60)
                transport_type = "car"
            else:
                travel_min = calc_walk_minutes(mapy, mapx, place_lat, place_lon)
                transport_type = "walk" if travel_min <= 15 else "car"

            a = _AMENITY_BY_TYPE[ptype]
            seen_ids.add(place_id)
            workplaces.append(_build_place(
                name=doc.get("place_name", ""),
                ptype=ptype,
                distance_min=travel_min,
                transport_type=transport_type,
                amenities=a,
            ))

    workplaces.sort(key=lambda w: w["distance_min"])
    return workplaces[:5]


def _search_dummy(mapx: float, mapy: float, radius_km: float) -> list[dict]:
    """카카오 API 실패 시 좌표 기반 더미 작업 공간 목록을 반환한다."""
    workplaces: list[dict] = []
    for name, ptype, dlat, dlon, *amenity_vals in _DUMMY_TEMPLATES:
        place_lat = mapy + dlat
        place_lon = mapx + dlon
        dist_km = calc_distance_km(mapy, mapx, place_lat, place_lon)
        if dist_km > radius_km:
            continue
        walk_min = calc_walk_minutes(mapy, mapx, place_lat, place_lon)
        workplaces.append(_build_place(
            name=name,
            ptype=ptype,
            distance_min=walk_min,
            transport_type="walk" if walk_min <= 15 else "car",
            amenities=tuple(amenity_vals),
        ))
    return workplaces[:5]


def _build_place(
    name: str,
    ptype: str,
    distance_min: int,
    transport_type: str,
    amenities: tuple,
) -> dict:
    """장소 딕셔너리를 표준 형태로 조립한다."""
    wifi, outlet, long_stay, quiet, large_table, pet, craft, parking = amenities  # quiet: True/False/None
    return {
        "name":           name,
        "type":           ptype,
        "distance_min":   distance_min,
        "transport_type": transport_type,
        "wifi":           wifi,
        "outlet":         outlet,
        "long_stay":      long_stay,
        "quiet":          quiet,
        "large_table":    large_table,
        "pet_friendly":   pet,
        "craft_allowed":  craft,
        "parking":        parking,
    }


def _to_float(value: object) -> float | None:
    """값을 float으로 변환한다. 실패 시 None을 반환한다."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
