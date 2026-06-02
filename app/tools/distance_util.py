import math


def calc_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 좌표 사이의 직선거리를 km 단위로 반환한다."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def calc_walk_minutes(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """직선거리 기반 도보 소요시간(분)을 반환한다. 도보 속도 4km/h 기준."""
    distance_km = calc_distance_km(lat1, lon1, lat2, lon2)
    return round(distance_km / 4 * 60)
