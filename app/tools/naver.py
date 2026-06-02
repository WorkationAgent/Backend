from __future__ import annotations
import httpx
from typing import Optional
from pydantic import BaseModel

from app.config.settings import (
    NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, NAVER_SEARCH_URL,
)


class NaverBlogPost(BaseModel):
    title: str
    description: str
    link: str
    postdate: Optional[str] = None


def _strip_tags(s: str) -> str:
    return (
        s.replace("<b>", "").replace("</b>", "")
         .replace("&quot;", '"').replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">")
    )


async def search_blog(query: str, display: int = 5) -> list[NaverBlogPost]:
    """블로그 후기 검색 – 분위기 보강용."""
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": min(display, 100), "sort": "sim"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(NAVER_SEARCH_URL, headers=headers, params=params)
        if r.status_code == 429:
            return []
        r.raise_for_status()
        data = r.json()

    return [
        NaverBlogPost(
            title=_strip_tags(item.get("title", "")),
            description=_strip_tags(item.get("description", "")),
            link=item.get("link", ""),
            postdate=item.get("postdate"),
        )
        for item in data.get("items", [])
    ]


async def search_blog_text(query: str, display: int = 5) -> str:
    """블로그 검색 결과를 텍스트로 합쳐서 반환한다. Stay Agent 전용."""
    posts = await search_blog(query, display)
    return " | ".join(p.description for p in posts if p.description)


async def search_region_reviews(parsed_preferences: dict, display: int = 5) -> str:
    """사용자 조건 기반 워케이션 지역 후기 검색. Stay Phase 1 전용."""
    region = parsed_preferences.get("desired_region", "")
    vibe = parsed_preferences.get("desired_vibe", "")
    purpose = parsed_preferences.get("purpose", "워케이션")
    query = f"{region} {vibe} {purpose} 한달살기 추천".strip()
    return await search_blog_text(query, display)


async def search_accommodation_reviews(accommodation_name: str, display: int = 3) -> str:
    """숙소명으로 블로그 후기 검색. Stay Phase 2 전용."""
    return await search_blog_text(f"{accommodation_name} 후기", display)
