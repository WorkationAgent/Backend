"""
생활 인프라 탐색 도구.

LivingSearchPlan(Planning LLM 출력)을 받아:
  1. Kakao Local (카테고리 코드 + 키워드) + Naver 로 후보 수집 (카테고리당 최대 5개)
  2. Kakao Mobility Directions API (자동차, 무료) 로 실제 도로 거리 + 소요 시간 계산
  3. 이동 방식별 필터:
       도보 — 도로 거리 ≤ 1,500m (재탐색 2,500m)
       자차 — 소요 시간 ≤ 3,600초 (재탐색 5,400초)
  4. 없으면 plan.retry_radius_km 반경으로 재탐색
"""

from __future__ import annotations

import asyncio
import functools
from math import cos, radians
from typing import Any, Dict, List, Literal, Optional, Tuple

import numpy as np

import pandas as pd
from app.tools.geo import haversine_meters as _haversine_geo

import httpx

from app.config.settings import (
    BUS_STOPS_CSV,
    MEDICAL_HOSPITALS_XLSX,
    MEDICAL_PHARMACIES_XLSX,
    KAKAO_LOCAL_URL,
    KAKAO_MOBILITY_URL,
    KAKAO_REST_API_KEY,
    NAVER_CLIENT_ID,
    NAVER_CLIENT_SECRET,
    RETRY_CAR_EXPAND_MIN,
    RETRY_RADIUS_EXPAND_KM,
    SEARCH_RADIUS_CAR_MIN,
    SEARCH_RADIUS_WALK_KM,
)

_CANDIDATES_PER_CATEGORY: int = 15  # 도보 모드에서 Directions API 제거로 비용 없음
from app.schemas.living_schema import (
    CategoryResult,
    CategorySearchPlan,
    LivingDetails,
    LivingSearchPlan,
    PlacePoint,
)

NAVER_LOCAL_URL = "https://openapi.naver.com/v1/search/local.json"

# 이동 방식별 필터 기준
_WALK_PRIMARY_M  = int(SEARCH_RADIUS_WALK_KM * 1000)                          # 1,500m
_WALK_RETRY_M    = int((SEARCH_RADIUS_WALK_KM + RETRY_RADIUS_EXPAND_KM) * 1000)  # 2,500m
_CAR_PRIMARY_SEC = SEARCH_RADIUS_CAR_MIN * 60                                  # 3,600초
_CAR_RETRY_SEC   = (SEARCH_RADIUS_CAR_MIN + RETRY_CAR_EXPAND_MIN) * 60        # 5,400초

_KAKAO_MAX_RADIUS_M = 20_000  # Kakao Local API 반경 상한


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 WGS84 좌표 사이 직선 거리(미터). 후보 수집용 넓은 반경에만 사용."""
    return _haversine_geo(lat1, lon1, lat2, lon2)


def _clean_html(text: str) -> str:
    return text.replace("<b>", "").replace("</b>", "")


# ── Kakao Mobility Directions ─────────────────────────────────────────────────

async def _road_info(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    client: httpx.AsyncClient,
) -> Tuple[int, int]:
    """
    자동차 길찾기 → (도로 거리 m, 소요 시간 초).
    도보 모드는 distance 기준, 자차 모드는 duration 기준으로 필터.
    경로를 못 찾으면 (-1, -1) 반환.
    """
    params  = {
        "origin":      f"{origin_lng},{origin_lat}",   # Kakao: 경도,위도 순
        "destination": f"{dest_lng},{dest_lat}",
        "priority":    "RECOMMEND",
    }
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    try:
        resp = await client.get(KAKAO_MOBILITY_URL, params=params, headers=headers, timeout=5.0)
        resp.raise_for_status()
        routes = resp.json().get("routes", [])
        if not routes or routes[0].get("result_code") != 0:
            return -1, -1
        s = routes[0]["summary"]
        return s["distance"], s["duration"]
    except Exception:
        return -1, -1


# ── Kakao Local 검색 ──────────────────────────────────────────────────────────

async def _kakao_category_search(
    code: str,
    lat: float,
    lng: float,
    radius_m: int,
    client: httpx.AsyncClient,
) -> List[PlacePoint]:
    params = {
        "category_group_code": code,
        "y": lat,
        "x": lng,
        "radius": min(radius_m, _KAKAO_MAX_RADIUS_M),
        "sort": "distance",
        "size": _CANDIDATES_PER_CATEGORY,
    }
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    try:
        resp = await client.get(
            f"{KAKAO_LOCAL_URL}/search/category.json",
            params=params, headers=headers, timeout=5.0,
        )
        resp.raise_for_status()
        docs = resp.json().get("documents", [])
    except Exception:
        return []

    places = []
    for doc in docs:
        try:
            places.append(PlacePoint(
                name=doc["place_name"],
                latitude=float(doc["y"]),
                longitude=float(doc["x"]),
                distance_meters=int(doc["distance"]),
            ))
        except (KeyError, ValueError):
            continue
    return places


async def _kakao_keyword_search(
    keyword: str,
    lat: float,
    lng: float,
    radius_m: int,
    client: httpx.AsyncClient,
) -> List[PlacePoint]:
    params = {
        "query": keyword,
        "y": lat,
        "x": lng,
        "radius": min(radius_m, _KAKAO_MAX_RADIUS_M),
        "sort": "distance",
        "size": _CANDIDATES_PER_CATEGORY,
    }
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    try:
        resp = await client.get(
            f"{KAKAO_LOCAL_URL}/search/keyword.json",
            params=params, headers=headers, timeout=5.0,
        )
        resp.raise_for_status()
        docs = resp.json().get("documents", [])
    except Exception:
        return []

    places = []
    for doc in docs:
        try:
            places.append(PlacePoint(
                name=doc["place_name"],
                latitude=float(doc["y"]),
                longitude=float(doc["x"]),
                distance_meters=int(doc["distance"]),
            ))
        except (KeyError, ValueError):
            continue
    return places


# ── Naver Local 검색 ──────────────────────────────────────────────────────────

_NAVER_DISPLAY_MAX = 5    # Naver Local API 단일 호출 최대 반환 건수
_NAVER_PAGES       = 2    # 페이지 수 (5 × 2 = 최대 10건)


async def _naver_search(
    keyword: str,
    lat: float,
    lng: float,
    radius_m: int,
    client: httpx.AsyncClient,
    area_name: str = "",
) -> List[PlacePoint]:
    """Naver Local 검색. display 최대 5건이라 start로 페이지네이션해 최대 15건 수집."""
    query   = f"{area_name} {keyword}".strip() if area_name else keyword
    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    raw_items: list = []
    for page in range(_NAVER_PAGES):
        params = {
            "query":   query,
            "display": _NAVER_DISPLAY_MAX,
            "start":   page * _NAVER_DISPLAY_MAX + 1,
            "sort":    "random",
        }
        try:
            resp = await client.get(NAVER_LOCAL_URL, params=params, headers=headers, timeout=5.0)
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if not items:
                break   # 더 이상 결과 없음
            raw_items.extend(items)
        except Exception:
            break

    places = []
    for item in raw_items:
        try:
            place_lng = int(item["mapx"]) / 1e7
            place_lat = int(item["mapy"]) / 1e7
            dist = _haversine(lat, lng, place_lat, place_lng)
            if dist <= radius_m * 2:
                places.append(PlacePoint(
                    name=_clean_html(item["title"]),
                    latitude=place_lat,
                    longitude=place_lng,
                    distance_meters=int(dist),
                ))
        except (KeyError, ValueError):
            continue
    return places


# ── 중복 제거 ─────────────────────────────────────────────────────────────────

def _deduplicate(kakao: List[PlacePoint], naver: List[PlacePoint]) -> List[PlacePoint]:
    """100m 이내 동일 위치 중복 제거. Kakao 우선."""
    merged = list(kakao)
    for np in naver:
        is_dup = any(
            _haversine(np.latitude, np.longitude, kp.latitude, kp.longitude) < 100
            for kp in merged
        )
        if not is_dup:
            merged.append(np)
    return merged


# ── Directions 필터 적용 ──────────────────────────────────────────────────────

async def _apply_directions_filter(
    candidates: List[PlacePoint],
    origin_lat: float,
    origin_lng: float,
    transport_mode: str,
    threshold: int,               # 도보: 거리(m), 자차: 시간(초)
    client: httpx.AsyncClient,
) -> List[PlacePoint]:
    """
    후보 장소를 이동 방식 기준으로 필터링.

    도보 모드: Haversine 직선 거리 사용.
      자동차 길찾기 API는 도보 경로와 달리 도로를 우회해
      실제 도보 거리보다 과대 측정됨 → 직선 거리가 더 정확한 근사.

    자차 모드: Kakao Mobility Directions API (소요 시간 기준).
      자동차는 도로를 따라야 하므로 실제 경로 시간이 필요.
      API가 -1 반환 시 Haversine fallback.
    """
    # 도보: Directions API 없이 Haversine 직접 필터
    if transport_mode != "car":
        filtered = []
        for place in candidates:
            dist = int(_haversine(origin_lat, origin_lng, place.latitude, place.longitude))
            if dist <= threshold:
                filtered.append(PlacePoint(
                    name=place.name,
                    latitude=place.latitude,
                    longitude=place.longitude,
                    distance_meters=dist,
                ))
        filtered.sort(key=lambda p: p.distance_meters)
        return filtered

    # 자차: Directions API 소요 시간 기준
    tasks = [
        _road_info(origin_lat, origin_lng, p.latitude, p.longitude, client)
        for p in candidates
    ]
    road_results = await asyncio.gather(*tasks)

    filtered = []
    for place, (road_dist, duration) in zip(candidates, road_results):
        if road_dist == -1:
            # 경로 없음 → Haversine fallback (소형 시설 등)
            road_dist = int(_haversine(origin_lat, origin_lng, place.latitude, place.longitude))
            duration  = -1

        if duration != -1 and duration <= threshold:
            filtered.append(PlacePoint(
                name=place.name,
                latitude=place.latitude,
                longitude=place.longitude,
                distance_meters=road_dist,
            ))

    filtered.sort(key=lambda p: p.distance_meters)
    return filtered


# ── 카테고리 단위 탐색 ────────────────────────────────────────────────────────

async def _search_category(
    cat_plan: CategorySearchPlan,
    origin_lat: float,
    origin_lng: float,
    radius_m: int,
    transport_mode: str,
    threshold: int,
    client: httpx.AsyncClient,
    area_name: str = "",
) -> CategoryResult:
    """후보 수집 → Directions 필터 → CategoryResult 반환."""
    code_tasks  = [_kakao_category_search(code, origin_lat, origin_lng, radius_m, client) for code in cat_plan.kakao_codes]
    kw_tasks    = [_kakao_keyword_search(kw,   origin_lat, origin_lng, radius_m, client) for kw   in cat_plan.kakao_keywords]
    naver_tasks = [_naver_search(kw, origin_lat, origin_lng, radius_m, client, area_name) for kw in cat_plan.naver_keywords]

    all_results = await asyncio.gather(*code_tasks, *kw_tasks, *naver_tasks)

    n_code = len(cat_plan.kakao_codes)
    n_kw   = len(cat_plan.kakao_keywords)

    kakao_raw: List[PlacePoint] = [p for r in all_results[: n_code + n_kw] for p in r]
    naver_raw: List[PlacePoint] = [p for r in all_results[n_code + n_kw :]  for p in r]

    # source는 Directions 필터 전 원본 기준으로 판별
    if   kakao_raw and naver_raw: source = "both"
    elif kakao_raw:               source = "kakao_only"
    elif naver_raw:               source = "naver_only"
    else:                         return CategoryResult(found=False, source="none")

    candidates = _deduplicate(kakao_raw, naver_raw)

    # Directions API로 실제 도로 거리/시간 기준 필터
    filtered = await _apply_directions_filter(
        candidates, origin_lat, origin_lng, transport_mode, threshold, client
    )

    if not filtered:
        return CategoryResult(found=False, source=source)

    nearest_dist = filtered[0].distance_meters
    nearest_min  = nearest_dist // 80  # 도보 추정 (자차도 참고용으로 유지)

    return CategoryResult(
        found=True,
        zone_km=radius_m / 1000,
        count=len(filtered),
        nearest_minutes=nearest_min,
        places=filtered[:10],
        source=source,
    )


# ── 공개 함수 ─────────────────────────────────────────────────────────────────

async def search_living_infra(
    lat: float,
    lng: float,
    plan: LivingSearchPlan,
    transport_mode: Literal["walk", "car"] = "walk",
    area_name: str = "",
) -> LivingDetails:
    """
    숙소 좌표 + Planning LLM 전략 기반 생활 인프라 탐색.

    Args:
        lat:            숙소 위도
        lng:            숙소 경도
        plan:           Planning LLM이 결정한 검색 전략 (LivingSearchPlan)
        transport_mode: 이동 방식 ("walk" | "car")

    Returns:
        LivingDetails
    """
    if transport_mode == "car":
        primary_threshold = _CAR_PRIMARY_SEC   # 3,600초
        retry_threshold   = _CAR_RETRY_SEC     # 5,400초
    else:
        primary_threshold = _WALK_PRIMARY_M    # 1,500m
        retry_threshold   = _WALK_RETRY_M      # 2,500m

    cat_plans: Dict[str, CategorySearchPlan] = {
        "transport": plan.transport,
        "grocery":   plan.grocery,
        "medical":   plan.medical,
        "services":  plan.services,
    }
    weights: Dict[str, float] = {name: getattr(plan, name).weight for name in cat_plans}
    category_results: Dict[str, CategoryResult] = {}

    primary_radius_m = int(plan.primary_radius_km * 1000)
    retry_radius_m   = int(plan.retry_radius_km   * 1000)

    async with httpx.AsyncClient() as client:
        for cat_name, cat_plan in cat_plans.items():
            result = await _search_category(
                cat_plan, lat, lng,
                primary_radius_m, transport_mode, primary_threshold, client, area_name,
            )
            if not result.found:
                result = await _search_category(
                    cat_plan, lat, lng,
                    retry_radius_m, transport_mode, retry_threshold, client, area_name,
                )
            category_results[cat_name] = result

    return LivingDetails(
        transport=category_results["transport"],
        grocery=category_results["grocery"],
        medical=category_results["medical"],
        services=category_results["services"],
        weights_applied=weights,
    )


async def search_categories(
    lat: float,
    lng: float,
    plan: LivingSearchPlan,
    transport_mode: Literal["walk", "car"],
    categories: List[str],
    use_retry_radius: bool = True,
    area_name: str = "",
) -> Dict[str, CategoryResult]:
    """
    특정 카테고리만 선택적으로 탐색. Reflection 후 재탐색에 사용.

    Args:
        categories:       탐색할 카테고리 목록 ["transport", "medical"]
        use_retry_radius: True면 plan.retry_radius_km 사용 (1차 탐색 실패한 카테고리 재탐색용)

    Returns:
        {category_name: CategoryResult}
    """
    if transport_mode == "car":
        primary_threshold = _CAR_PRIMARY_SEC
        retry_threshold   = _CAR_RETRY_SEC
    else:
        primary_threshold = _WALK_PRIMARY_M
        retry_threshold   = _WALK_RETRY_M

    radius_m  = int((plan.retry_radius_km if use_retry_radius else plan.primary_radius_km) * 1000)
    threshold = retry_threshold if use_retry_radius else primary_threshold

    cat_plans: Dict[str, CategorySearchPlan] = {
        "transport": plan.transport,
        "grocery":   plan.grocery,
        "medical":   plan.medical,
        "services":  plan.services,
    }

    results: Dict[str, CategoryResult] = {}
    async with httpx.AsyncClient() as client:
        for cat_name in categories:
            if cat_name not in cat_plans:
                continue
            result = await _search_category(
                cat_plans[cat_name], lat, lng, radius_m, transport_mode, threshold, client, area_name,
            )
            results[cat_name] = result

    return results


_SCAN_CODES: Dict[str, List[str]] = {
    "transport": ["SW8", "BS8"],
    "grocery":   ["MT1", "CS2"],
    "medical":   ["HP8", "PM9"],
    "services":  ["BK9", "PO3"],
}


async def quick_scan(
    lat: float,
    lng: float,
    scan_radius_km: float,
) -> Dict[str, Any]:
    """
    Planning LLM 호출 전 실제 인프라 현황 파악을 위한 사전 탐색.

    Kakao 카테고리 검색 + 반경 필터 (Directions API 없음 — 빠름).
    카테고리별 존재 여부·개수·최근접 직선 거리만 반환.

    Args:
        lat:            숙소 위도
        lng:            숙소 경도
        scan_radius_km: 탐색 반경 (도보: ~3km, 자차: ~10km 권장)

    Returns:
        {
            "transport": {"found": bool, "count": int, "nearest_m": int | None},
            "grocery":   {...},
            "medical":   {...},
            "services":  {...},
            "scan_radius_km": float,
        }
    """
    radius_m = min(int(scan_radius_km * 1000), _KAKAO_MAX_RADIUS_M)

    async with httpx.AsyncClient() as client:
        raw: Dict[str, List[PlacePoint]] = {}
        for cat, codes in _SCAN_CODES.items():
            results = await asyncio.gather(*[
                _kakao_category_search(code, lat, lng, radius_m, client) for code in codes
            ])
            raw[cat] = [p for lst in results for p in lst]

    result: Dict[str, Any] = {"scan_radius_km": scan_radius_km}
    for cat, places in raw.items():
        if places:
            nearest = min(p.distance_meters for p in places)
            result[cat] = {"found": True, "count": len(places), "nearest_m": nearest}
        else:
            result[cat] = {"found": False, "count": 0, "nearest_m": None}

    return result


async def reverse_geocode(lat: float, lng: float) -> str:
    """
    좌표 → 동/읍/면 이름. Naver 검색 prefix 용도.

    Returns:
        지역명 문자열 (예: "교동", "대관령면"). 실패 시 빈 문자열.
    """
    params  = {"x": lng, "y": lat, "input_coord": "WGS84"}
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{KAKAO_LOCAL_URL}/geo/coord2regioncode.json",
                params=params, headers=headers, timeout=5.0,
            )
            resp.raise_for_status()
            for doc in resp.json().get("documents", []):
                if doc.get("region_type") == "B":
                    city = doc.get("region_2depth_name", "")
                    dong = doc.get("region_4depth_name") or doc.get("region_3depth_name", "")
                    return f"{city} {dong}".strip() if dong else city
        except Exception:
            pass
    return ""


async def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    주소 문자열 → (위도, 경도). candidate_accommodations에 좌표 없을 때 사용.

    Returns:
        (latitude, longitude) 또는 None
    """
    params  = {"query": address, "size": 1}
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{KAKAO_LOCAL_URL}/search/address.json",
                params=params, headers=headers, timeout=5.0,
            )
            resp.raise_for_status()
            docs = resp.json().get("documents", [])
            if not docs:
                return None
            doc = docs[0]
            return float(doc["y"]), float(doc["x"])
        except Exception:
            return None


_WALK_MAX_THRESHOLD_M = 5000  # 도보 threshold 상한 (약 60분)


async def search_one_category(
    lat: float,
    lng: float,
    kakao_codes: List[str],
    keywords: List[str],
    radius_m: int,
    transport_mode: Literal["walk", "car"] = "walk",
    area_name: str = "",
    naver_keywords: List[str] = [],
) -> CategoryResult:
    """ReAct Agent용 단일 카테고리 탐색.

    threshold:
      도보 — LLM이 요청한 radius_m을 그대로 사용 (상한 5000m).
             LLM이 radius_m을 늘리면 필터 기준도 함께 넓어진다.
      자차 — 시간 기준(_CAR_PRIMARY_SEC) 고정 (거리↔시간 환산 불명확).

    Directions API가 -1을 반환하면 Haversine 직선 거리로 fallback.
    """
    cat_plan = CategorySearchPlan(
        kakao_codes=kakao_codes,
        kakao_keywords=keywords,
        naver_keywords=naver_keywords,
        weight=1.0,
        priority="essential",
    )
    threshold = min(radius_m, _WALK_MAX_THRESHOLD_M) if transport_mode == "walk" else _CAR_PRIMARY_SEC
    async with httpx.AsyncClient() as client:
        return await _search_category(
            cat_plan, lat, lng, radius_m, transport_mode, threshold, client, area_name,
        )


# ── 공통 numpy 반경 검색 ─────────────────────────────────────────────────────

def _search_near(
    df: pd.DataFrame,
    lat: float,
    lng: float,
    radius_m: int,
    name_col: str,
    max_results: int,
) -> List[PlacePoint]:
    """bbox 선필터 → numpy vectorized Haversine → 거리 순 반환.

    1. 위경도 ±δ 사각형으로 후보를 수백 건으로 축소 (O(n) 비교 → 빠름)
    2. 남은 후보에만 정확한 Haversine 적용 (numpy 배열 연산)
    3. radius_m 이하만 거리 순 정렬
    """
    if df.empty:
        return []

    # ── 1. bbox 선필터 ────────────────────────────────────────────────────────
    dlat = radius_m / 111_111                         # 위도 1° ≈ 111.1km
    dlng = radius_m / (111_111 * cos(radians(lat)))   # 경도 1° ≈ 111.1km × cos(lat)

    box = df[
        (df["위도"] >= lat - dlat) & (df["위도"] <= lat + dlat) &
        (df["경도"] >= lng - dlng) & (df["경도"] <= lng + dlng)
    ]
    if box.empty:
        return []

    # ── 2. vectorized Haversine ────────────────────────────────────────────────
    lat_r  = radians(lat)
    lats   = np.radians(box["위도"].to_numpy())
    lngs   = np.radians(box["경도"].to_numpy())
    dlat_a = lats - lat_r
    dlng_a = lngs - radians(lng)

    a    = np.sin(dlat_a / 2) ** 2 + cos(lat_r) * np.cos(lats) * np.sin(dlng_a / 2) ** 2
    dist = (2 * 6_371_000 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))).astype(int)

    # ── 3. 반경 필터 + 정렬 ────────────────────────────────────────────────────
    mask  = dist <= radius_m
    names = box[name_col].to_numpy()[mask]
    lats_ = box["위도"].to_numpy()[mask]
    lngs_ = box["경도"].to_numpy()[mask]
    dists = dist[mask]

    order = np.argsort(dists)[:max_results]
    return [
        PlacePoint(
            name=str(names[i]),
            latitude=float(lats_[i]),
            longitude=float(lngs_[i]),
            distance_meters=int(dists[i]),
        )
        for i in order
    ]


# ── 버스정류장 CSV 검색 ───────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _load_bus_stops() -> pd.DataFrame:
    """CSV를 앱 최초 호출 시 한 번만 로딩 후 메모리 캐싱."""
    try:
        df = pd.read_csv(BUS_STOPS_CSV, encoding="utf-8-sig", dtype=str)
        df["위도"] = pd.to_numeric(df["위도"], errors="coerce")
        df["경도"] = pd.to_numeric(df["경도"], errors="coerce")
        df = df.dropna(subset=["위도", "경도"])
        return df
    except Exception:
        return pd.DataFrame(columns=["정류장번호", "정류장명", "위도", "경도", "도시명"])


def search_bus_stops_near(
    lat: float,
    lng: float,
    radius_m: int,
    city_name: str = "",
) -> List[PlacePoint]:
    """CSV에서 반경 내 버스정류장 반환 (numpy 벡터화)."""
    df = _load_bus_stops()
    if df.empty:
        return []

    if city_name:
        filtered = df[df["도시명"].str.contains(city_name, na=False)]
        if filtered.empty:
            filtered = df
    else:
        filtered = df

    return _search_near(filtered, lat, lng, radius_m, name_col="정류장명", max_results=10)


# ── 의료기관 XLSX 검색 ────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _load_medical() -> pd.DataFrame:
    """병원 + 약국 XLSX를 최초 호출 시 한 번만 로딩 후 메모리 캐싱."""
    dfs = []
    for path in [MEDICAL_HOSPITALS_XLSX, MEDICAL_PHARMACIES_XLSX]:
        try:
            df = pd.read_excel(path, dtype=str)
            df = df[["요양기관명", "시도코드명", "시군구코드명", "주소", "좌표(X)", "좌표(Y)"]].copy()
            df.rename(columns={"좌표(X)": "경도", "좌표(Y)": "위도"}, inplace=True)
            df["위도"] = pd.to_numeric(df["위도"], errors="coerce")
            df["경도"] = pd.to_numeric(df["경도"], errors="coerce")
            df = df.dropna(subset=["위도", "경도"])
            dfs.append(df)
        except Exception:
            pass
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame(
        columns=["요양기관명", "시도코드명", "시군구코드명", "주소", "위도", "경도"]
    )


def search_medical_near(
    lat: float,
    lng: float,
    radius_m: int,
    sido: str = "",
) -> List[PlacePoint]:
    """XLSX에서 반경 내 의료기관(병원+약국) 반환.

    sido(시도코드명)로 선필터 후 Haversine 계산.
    예: sido="제주" → 제주특별자치도만 검색.
    """
    df = _load_medical()
    if df.empty:
        return []

    if sido:
        filtered = df[df["시도코드명"].str.contains(sido, na=False)]
        if filtered.empty:
            filtered = df
    else:
        filtered = df

    return _search_near(filtered, lat, lng, radius_m, name_col="요양기관명", max_results=15)
