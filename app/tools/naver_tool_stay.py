import httpx
from app.config.settings import NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, NAVER_SEARCH_URL


def _search_blog(query: str, max_results: int) -> str:
    """네이버 블로그 검색 후 텍스트를 합쳐서 반환하는 공통 함수."""
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {
        "query": query,
        "display": max_results,
        "sort": "sim",
    }
    try:
        resp = httpx.get(NAVER_SEARCH_URL, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        texts = []
        for item in items:
            desc = item.get("description", "").replace("<b>", "").replace("</b>", "")
            if desc:
                texts.append(desc)
        return " | ".join(texts)
    except Exception:
        return ""


def search_region_reviews(parsed_preferences: dict, max_results: int = 5) -> str:
    """사용자 조건 기반으로 워케이션 지역 후기를 검색한다. Phase 1에서 사용."""
    region = parsed_preferences.get("desired_region", "")
    vibe = parsed_preferences.get("desired_vibe", "")
    purpose = parsed_preferences.get("purpose", "워케이션")
    query = f"{region} {vibe} {purpose} 한달살기 추천".strip()
    return _search_blog(query, max_results)


def search_reviews(accommodation_name: str, max_results: int = 3) -> str:
    """숙소명으로 네이버 블로그 후기를 검색하고 텍스트로 합쳐서 반환한다. Phase 2에서 사용."""
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {
        "query": f"{accommodation_name} 후기",
        "display": max_results,
        "sort": "sim",
    }

    return _search_blog(f"{accommodation_name} 후기", max_results)
