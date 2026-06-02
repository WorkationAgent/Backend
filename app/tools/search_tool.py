"""
search_tool.py — 네이버 블로그 검색 기반 작업 공간 후기 보완 도구

카카오 로컬 API가 제공하지 못하는 실제 작업 환경 정보
(Wi-Fi 품질·콘센트 유무·장시간 이용 여부 등)를
네이버 블로그 후기에서 추출해 place_tool의 추정값을 보정한다.
"""

import re

import httpx

from app.config.settings import (
    NAVER_CLIENT_ID,
    NAVER_CLIENT_SECRET,
    NAVER_SEARCH_URL,
)

# 긍정/부정 키워드 사전
_WIFI_POS  = ["와이파이 빠", "와이파이 잘", "wifi 빠", "wifi 잘", "와이파이 좋", "인터넷 빠", "와이파이 빵빵"]
_WIFI_NEG  = ["와이파이 느", "와이파이 없", "wifi 없", "인터넷 느", "와이파이 불안"]

_OUTLET_POS = ["콘센트 있", "콘센트 많", "충전 가능", "플러그 있"]
_OUTLET_NEG = ["콘센트 없", "충전 못", "콘센트 부족"]

_LONGSTAY_POS = ["오래 앉", "오래 있", "장시간", "눈치 없", "편하게 오래", "오랫동안"]
_LONGSTAY_NEG = ["눈치 보", "오래 있기 힘", "회전율", "빨리 나가", "오래 못 있"]


def search_workplace_reviews(
    place_name: str,
    region_name: str,
) -> dict:
    """네이버 블로그에서 작업 공간 후기를 검색하고 환경 정보를 추출한다.

    카카오 API의 편의시설 추정값(wifi, outlet 등)을 실제 방문자 후기로 보정하기 위해
    사용한다. 검색어는 "{지역명} {장소명} 와이파이" 형태로 구성한다.

    Args:
        place_name:  장소명 (예: "카페 그린")
        region_name: 지역명 (예: "제주 세화리"). 검색 정확도를 높이기 위해 사용.

    Returns:
        다음 키를 포함하는 딕셔너리:
            - raw_review (str): 블로그 후기 원문 (LLM 추가 분석용)
            - wifi (bool | None): 와이파이 양호 여부. 언급 없으면 None.
            - outlet (bool | None): 콘센트 이용 가능 여부. 언급 없으면 None.
            - long_stay (bool | None): 장시간 이용 가능 여부. 언급 없으면 None.
        API 호출 실패 또는 검색 결과 없을 때는 빈 딕셔너리를 반환한다.
    """
    query = f"{region_name} {place_name} 와이파이"
    raw_text = _fetch_blog_text(query)
    if not raw_text:
        return {}

    return {
        "raw_review": raw_text,
        "wifi":       _extract_feature(raw_text, _WIFI_POS, _WIFI_NEG),
        "outlet":     _extract_feature(raw_text, _OUTLET_POS, _OUTLET_NEG),
        "long_stay":  _extract_feature(raw_text, _LONGSTAY_POS, _LONGSTAY_NEG),
    }


def _fetch_blog_text(query: str, max_results: int = 3) -> str:
    """네이버 블로그 검색 후 결과 텍스트를 하나의 문자열로 합쳐 반환한다."""
    try:
        headers = {
            "X-Naver-Client-Id":     NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        }
        params = {
            "query":   query,
            "display": max_results,
            "sort":    "sim",
        }
        resp = httpx.get(NAVER_SEARCH_URL, headers=headers, params=params, timeout=10)
        resp.raise_for_status()

        items = resp.json().get("items", [])
        texts: list[str] = []
        for item in items:
            desc = re.sub(r"<[^>]+>", "", item.get("description", ""))
            if desc:
                texts.append(desc)
        return " | ".join(texts)
    except Exception:
        return ""


def _extract_feature(
    text: str,
    positive_keywords: list[str],
    negative_keywords: list[str],
) -> bool | None:
    """텍스트에서 특정 기능의 긍정/부정 언급 여부를 판단한다.

    긍정 키워드가 먼저 매칭되면 True, 부정 키워드가 먼저 매칭되면 False를 반환한다.
    아무것도 매칭되지 않으면 None을 반환해 '언급 없음'으로 처리한다.
    """
    lower = text.lower()

    pos_idx = min(
        (lower.find(kw) for kw in positive_keywords if lower.find(kw) != -1),
        default=None,
    )
    neg_idx = min(
        (lower.find(kw) for kw in negative_keywords if lower.find(kw) != -1),
        default=None,
    )

    if pos_idx is None and neg_idx is None:
        return None
    if pos_idx is None:
        return False
    if neg_idx is None:
        return True
    return pos_idx < neg_idx  # 먼저 나온 쪽이 우세
