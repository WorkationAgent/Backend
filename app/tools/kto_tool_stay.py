import httpx
from app.config.settings import KTO_API_KEY, KTO_BASE_URL


def search_accommodations(region_name: str, max_rows: int = 20) -> list[dict]:
    """KTO API로 숙박 시설을 검색한다.

    KTO는 시/도 단위(제주, 강릉 등)로만 결과가 나오므로
    생활권명의 첫 번째 단어(시/도)로 검색한 뒤
    LLM이 생활권에 맞는 숙소를 필터링한다.
    """
    cleaned = region_name.replace("생활권", "").strip()
    words = cleaned.split()

    # 첫 번째 단어(시/도) → 없으면 두 번째 단어까지 시도
    keywords = words[:2]

    for keyword in keywords:
        result = _search_by_keyword(keyword, max_rows)
        if result:
            return result

    return []


def _search_by_keyword(keyword: str, max_rows: int) -> list[dict]:
    params = {
        "serviceKey": KTO_API_KEY,
        "numOfRows": max_rows,
        "pageNo": 1,
        "MobileOS": "ETC",
        "MobileApp": "WorkationPlanner",
        "_type": "json",
        "contentTypeId": 32,  # 숙박
        "keyword": keyword,
    }

    try:
        resp = httpx.get(f"{KTO_BASE_URL}/searchKeyword2", params=params, timeout=10)
        resp.raise_for_status()
        body = resp.json().get("response", {}).get("body", {})
        items = body.get("items", {})
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
