"""RAG 인덱스 오프라인 빌더 (지역 특색 / 워케이션 맥락).

수집 = 혼합:
  - 위키백과(자동): 시군구 문서 요약(extract) → LLM이 facet 청크로 정제
  - URL 출처(data/rag_sources.json): 지역별 URL 본문 추출 → LLM이 facet 청크로 정제 (source=URL)
  - 멀티지역 URL("_multi"): 여러 지역을 다루는 페이지 → LLM이 청크마다 시군구를 판별해 적재
  - 수동 시드(data/rag_seed.json): 이미 facet 태깅된 청크를 그대로 적재(LLM 미경유)

적재 = idempotent upsert (rag.upsert_chunks). 재실행해도 중복 없이 갱신만.
요청 경로(에이전트)는 읽기만 하므로, 데이터 갱신이 필요할 때만 이 스크립트를 돌린다.

사용:
    python scripts/build_rag_index.py                 # 파일럿 지역 전체 (시드+위키+URL)
    python scripts/build_rag_index.py 강릉시 양양군     # 일부 지역만
    python scripts/build_rag_index.py --seed-only      # 시드만
    python scripts/build_rag_index.py --wiki-only      # 위키만
    python scripts/build_rag_index.py --url-only       # 지역별 URL만
    python scripts/build_rag_index.py --multi-only     # 멀티지역 URL(_multi)만

수집 경로 3종 + 멀티:
  - 시드/위키/지역별URL : data/rag_sources.json 의 "지역명": [url...] (그 지역으로 태깅)
  - 멀티지역 URL        : data/rag_sources.json 의 "_multi": [url...]
                          (한 페이지가 여러 지역을 다룰 때 — LLM이 청크마다 시군구 판별)
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import re
import sys
from datetime import date
from urllib.parse import quote

import httpx
from pydantic import BaseModel

# 스크립트를 Backend/ 기준으로 import 가능하게
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app.config.settings import RAG_SEED_JSON, RAG_SOURCES_JSON  # noqa: E402
from app.core.llm import call_llm  # noqa: E402
from app.tools.rag import (  # noqa: E402
    RegionalContextChunk,
    _sigungu_core,
    delete_region_source,
    delete_source,
    upsert_chunks,
)


# 파일럿 지역 (워케이션 인기 지역)
PILOT_REGIONS = ["제주시", "서귀포시", "강릉시", "양양군", "남해군", "경주시"]

_TODAY = date.today().isoformat()
_UA = "WokationAgent-RAG-Builder/1.0 (workation recommender; contact: wltn7896@gmail.com)"

# HTML → 텍스트 추출용
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


# ── 위키 정제 LLM 스키마 ───────────────────────────────────────────────
class _Chunk(BaseModel):
    text: str
    facet: list[str] = []          # ["work"] | ["local"] | 둘 다
    themes: list[str] = []
    vibe_tags: list[str] = []
    hobby_tags: list[str] = []
    season_peak: list[str] = []
    workation_fit: str = ""


class _Batch(BaseModel):
    chunks: list[_Chunk]


_NORMALIZE_SYSTEM = """당신은 워케이션 추천 서비스의 지역 지식 큐레이터입니다.
주어진 지역 설명 텍스트를, RAG 검색에 쓸 작은 청크 여러 개로 정제합니다.

[규칙]
- 각 청크는 1~3문장, 하나의 테마만 담는다.
- 4~8개 생성한다.
- 제공된 텍스트에 근거한 사실만 쓴다. 없는 사실(특정 가게명·수치·지원사업명)을 지어내지 마라.
- facet 태깅:
  - "local": 지역 정체성·명소·분위기·자연·계절·취미 등 '지역 경험'
  - "work":  원격근무·코워킹·작업하기 좋은 카페 문화·장기체류 여건 등 '워케이션'
  - 둘 다 해당하면 ["work","local"]
- 개별 장소의 좌표·전화·영업시간 같은 휘발성 정보는 담지 마라(그건 실시간 API의 몫).
- 한국어로 작성한다.
반드시 유효한 JSON만 반환하세요."""


def _normalize_user(region: str, extract: str) -> str:
    return f"""[지역] {region}

[설명 텍스트]
{extract}

위 텍스트를 facet 태깅된 청크 목록으로 정제하세요."""


async def _fetch_wiki_extract(title: str) -> str:
    """한국어 위키백과 요약 extract (키 불필요 REST API).

    Wikimedia 정책상 설명적 User-Agent가 필수다(없으면 403).
    """
    url = f"https://ko.wikipedia.org/api/rest_v1/page/summary/{quote(title)}"
    headers = {
        "accept": "application/json",
        "user-agent": "WokationAgent-RAG-Builder/1.0 (workation recommender; contact: wltn7896@gmail.com)",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
            if r.status_code != 200:
                print(f"   · 위키 HTTP {r.status_code}: {title}")
                return ""
            return r.json().get("extract", "") or ""
    except Exception as e:
        print(f"   · 위키 요청 실패({title}): {e}")
        return ""


def _load_seed() -> dict:
    try:
        with open(RAG_SEED_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[!] 시드 로드 실패: {e}")
        return {}


async def _seed_chunks(region: str, seed: dict) -> list[RegionalContextChunk]:
    entries = seed.get(region, [])
    out = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("text"):
            continue
        out.append(RegionalContextChunk(
            region=region, source="seed", source_date=_TODAY, **e,
        ))
    return out


async def _wiki_chunks(region: str) -> list[RegionalContextChunk]:
    extract = await _fetch_wiki_extract(region)
    if not extract:
        print(f"   · 위키 extract 없음: {region}")
        return []
    try:
        batch: _Batch = await call_llm(
            messages=[{"role": "user", "content": _normalize_user(region, extract)}],
            system=_NORMALIZE_SYSTEM,
            output_schema=_Batch,
            max_tokens=1500,
        )
    except Exception as e:
        print(f"   · 위키 정제 LLM 실패({region}): {e}")
        return []
    return [
        RegionalContextChunk(
            region=region, source="wikipedia", source_date=_TODAY, **c.model_dump(),
        )
        for c in batch.chunks if c.text.strip()
    ]


def _load_sources() -> dict:
    try:
        with open(RAG_SOURCES_JSON, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[!] 출처 URL 로드 실패: {e}")
        return {}


async def _extract_url_text(url: str) -> str:
    """URL 본문을 평문으로 추출 (태그·스크립트 제거). 정적/기사형 페이지에 적합."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            r = await c.get(url, headers={"user-agent": _UA})
            if r.status_code != 200:
                print(f"   · URL HTTP {r.status_code}: {url}")
                return ""
            raw = r.text
    except Exception as e:
        print(f"   · URL 요청 실패({url}): {e}")
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:4000]


async def _url_chunks(region: str, urls: list[str]) -> list[RegionalContextChunk]:
    """지역 출처 URL들 → 본문 추출 → LLM facet 정제. source=각 URL."""
    out: list[RegionalContextChunk] = []
    for url in urls:
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        text = await _extract_url_text(url)
        if len(text) < 200:
            print(f"   · URL 본문 부족/추출 실패: {url}")
            continue
        try:
            batch: _Batch = await call_llm(
                messages=[{"role": "user", "content": _normalize_user(region, text)}],
                system=_NORMALIZE_SYSTEM,
                output_schema=_Batch,
                max_tokens=1500,
            )
        except Exception as e:
            print(f"   · URL 정제 LLM 실패({url}): {e}")
            continue
        out += [
            RegionalContextChunk(
                region=region, source=url, source_date=_TODAY, **c.model_dump(),
            )
            for c in batch.chunks if c.text.strip()
        ]
    return out


# ── 멀티지역 URL (전국/도 단위 페이지) — 청크마다 LLM이 시군구를 판별 ──────────
class _MultiChunk(BaseModel):
    region: str = ""               # 이 청크가 다루는 시·군·구 (예: "강릉시")
    text: str = ""
    facet: list[str] = []
    themes: list[str] = []
    vibe_tags: list[str] = []
    hobby_tags: list[str] = []
    season_peak: list[str] = []
    workation_fit: str = ""


class _MultiBatch(BaseModel):
    chunks: list[_MultiChunk]


_MULTI_SYSTEM = """당신은 워케이션 추천 서비스의 지역 지식 큐레이터입니다.
여러 지역을 다루는 텍스트에서, '특정 시·군·구'에 관한 내용만 골라 작은 청크로 정제합니다.

[규칙]
- 각 청크는 1~3문장, 하나의 테마만.
- region에는 그 청크가 다루는 시·군·구명을 접미사까지 정확히 적는다 (예: "강릉시", "여수시", "순천시").
- 특정 시·군·구를 특정할 수 없는 일반 내용(워케이션 일반론, 준비물, 회사 소개 등)은 청크로 만들지 마라.
- 제공된 텍스트에 근거한 사실만. 없는 사실(가게명·가격·예약 정보 등)을 지어내지 마라.
- 개별 장소의 좌표·전화·영업시간·가격 같은 휘발성 정보는 담지 마라(실시간 API의 몫).
- facet 태깅:
  - "local": 지역 정체성·명소·분위기·자연·계절·취미
  - "work":  원격근무·코워킹·작업 카페 문화·장기체류 여건
  - 둘 다면 ["work","local"]
- 한국어로 작성한다.
반드시 유효한 JSON만 반환하세요."""


def _multi_user(text: str) -> str:
    return f"""[텍스트]
{text}

위 텍스트에서 특정 시·군·구에 관한 내용만 region을 명시한 청크 목록으로 정제하세요."""


def _load_valid_cores() -> set[str]:
    """area_codes.json의 모든 시·군·구 핵심부 집합 — LLM이 뱉은 지역 검증용."""
    import pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "app" / "tools" / "area_codes.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[!] area_codes.json 로드 실패 — 지역 검증 생략: {e}")
        return set()
    cores: set[str] = set()
    for sido in data.values():
        for name in (sido.get("sigungu") or {}).values():
            core = _sigungu_core(name)
            if core:
                cores.add(core)
    return cores


async def _multi_chunks(urls: list[str]) -> list[RegionalContextChunk]:
    """멀티지역 URL들 → 본문 추출 → LLM이 청크마다 시군구 판별. source=각 URL.

    LLM이 지정한 region이 실제 시·군·구(area_codes.json)일 때만 채택.
    """
    valid = _load_valid_cores()
    out: list[RegionalContextChunk] = []
    for url in urls:
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        text = await _extract_url_text(url)
        if len(text) < 200:
            print(f"   · URL 본문 부족/추출 실패: {url}")
            continue
        try:
            batch: _MultiBatch = await call_llm(
                messages=[{"role": "user", "content": _multi_user(text)}],
                system=_MULTI_SYSTEM,
                output_schema=_MultiBatch,
                max_tokens=2000,
            )
        except Exception as e:
            print(f"   · 멀티 정제 LLM 실패({url}): {e}")
            continue
        kept = 0
        for c in batch.chunks:
            if not c.text.strip() or not c.region.strip():
                continue
            region_clean = c.region.strip()
            core = _sigungu_core(region_clean)
            # 실제 시·군·구만 채택 — 도(道) 단위/일반론/미인식 지역은 버림.
            if not core or not region_clean.endswith(("시", "군", "구")):
                continue
            if valid and core not in valid:
                continue  # area_codes.json이 있으면 실제 코드로 한 번 더 검증
            d = c.model_dump()
            d.pop("region")
            out.append(RegionalContextChunk(
                region=c.region, source=url, source_date=_TODAY, **d,
            ))
            kept += 1
        print(f"   · {url[:50]}… → 채택 {kept}개")
    return out


async def build(
    regions: list[str],
    use_seed: bool,
    use_wiki: bool,
    use_url: bool,
    run_multi: bool,
) -> None:
    seed = _load_seed() if use_seed else {}
    # 로드 실패/빈 파일이면 시드 단계를 건너뛴다(파싱 오류로 기존 시드 청크를 날리지 않게).
    process_seed = use_seed and bool(seed)
    sources = _load_sources() if (use_url or run_multi) else {}
    total = 0
    do_region_loop = process_seed or use_wiki or use_url
    for region in (regions if do_region_loop else []):
        print(f"▶ {region}")
        chunks: list[RegionalContextChunk] = []
        if process_seed:
            sc = await _seed_chunks(region, seed)
            # 시드도 지역 단위로 교체 — text 수정/항목 삭제가 그대로 반영되도록.
            delete_region_source(region, "seed")
            chunks += sc
            print(f"   · 시드 청크 {len(sc)}개")
        if use_wiki:
            wc = await _wiki_chunks(region)
            if wc:
                # 새 위키 청크가 생겼을 때만 교체 (전송 실패로 기존 데이터 날리지 않게)
                delete_region_source(region, "wikipedia")
            chunks += wc
            print(f"   · 위키 청크 {len(wc)}개")
        if use_url:
            uc = await _url_chunks(region, sources.get(region, []))
            # 이번에 성공적으로 받아온 URL만 교체(idempotent). 실패한 URL의 기존 청크는 보존.
            for u in {c.source for c in uc}:
                delete_region_source(region, u)
            chunks += uc
            print(f"   · URL 청크 {len(uc)}개")
        n = await upsert_chunks(chunks)
        total += n
        print(f"   → upsert {n}개")

    # 멀티지역 URL — 지역 루프와 무관하게 1회. 청크마다 LLM이 시군구를 판별해 적재.
    if run_multi:
        multi_urls = [
            u for u in (sources.get("_multi") or [])
            if isinstance(u, str) and u.startswith("http")
        ]
        if multi_urls:
            print("▶ _multi (여러 지역 출처)")
            mc = await _multi_chunks(multi_urls)
            # 받아온 URL은 source 단위로 전부 교체(여러 지역에 걸쳐 있으므로).
            for u in {c.source for c in mc}:
                delete_source(u)
            n = await upsert_chunks(mc)
            total += n
            regions_hit = sorted({c.region for c in mc})
            print(f"   → upsert {n}개 (지역 {len(regions_hit)}곳: {regions_hit})")

    print(f"\n✅ 완료: 총 {total}개 청크 적재/갱신")


def main() -> None:
    args = list(sys.argv[1:])
    only = next(
        (a for a in args if a in ("--seed-only", "--wiki-only", "--url-only", "--multi-only")),
        None,
    )
    use_seed = only in (None, "--seed-only")
    use_wiki = only in (None, "--wiki-only")
    use_url = only in (None, "--url-only")
    region_args = [a for a in args if not a.startswith("--")]
    regions = region_args or PILOT_REGIONS
    # 멀티지역은 지역 인자와 무관하므로, 특정 지역만 지정한 빌드에서는 돌리지 않는다
    # (전체 빌드 또는 --multi-only 일 때만).
    run_multi = (only in (None, "--multi-only")) and not region_args
    asyncio.run(build(regions, use_seed, use_wiki, use_url, run_multi))


if __name__ == "__main__":
    main()
