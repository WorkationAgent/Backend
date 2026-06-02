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
