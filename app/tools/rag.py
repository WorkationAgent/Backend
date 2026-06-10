"""지역 특색 / 워케이션 맥락 RAG 검색 (v2).

설계 (합의):
  - 단일 코퍼스 + facet 분리. 한 청크가 work/local 양쪽에 유효할 수 있으므로
    인덱스는 하나(Chroma 'regional_context')만 두고, 검색 시 facet으로 가른다.
        retrieve_regional_context() → facet=local (지역 경험: 정체성·명소·분위기·계절)
        retrieve_work_context()     → facet=work  (원격근무: 코워킹·작업 카페 문화·장기체류)
  - 적재(쓰기)와 검색(읽기)을 분리. 데이터는 오프라인 빌더(scripts/build_rag_index.py)가
    사전 구축하고, 요청 경로에서는 읽기만 한다. 매 실행 적재 금지.
  - idempotent upsert: 안정 ID(region+source+text 해시)로 재실행해도 중복 없이 갱신만.

RAG는 '옵션'이다 — 인덱스가 없거나 chromadb 미설치/오류여도 절대 예외를 던지지 않고
빈 리스트를 반환한다. (local_agent의 _collect_signals는 gather에 return_exceptions를
쓰지 않으므로, 여기서 예외가 새면 평가 전체가 깨진다.)

저장 형식 메모: Chroma 메타데이터는 스칼라만 허용한다. 따라서
  - facet 은 facet_work / facet_local 불린 플래그로 저장하고 그걸로 where 필터.
  - themes 등 리스트 필드는 chunk_json(전체 직렬화)에 담아 복원한다.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional

from pydantic import BaseModel

from app.config.settings import RAG_CHROMA_DIR, RAG_COLLECTION
from app.core.llm import get_embeddings

logger = logging.getLogger(__name__)

_SIGUNGU_RE = re.compile(r"([가-힣]+?)(특별자치시|특별자치도|광역시|특별시|시|군|구)(?:\b|$)")


def _sigungu_core(region: str) -> str:
    """행정구역 문자열에서 시·군·구 핵심부를 뽑는다 (포맷 차이 흡수).

    "강원도 양양군" / "양양군" / "양양군 죽도 일대" → "양양"
    "제주특별자치도 서귀포시" → "서귀포" (제주시와 혼동 방지)
    매칭 실패 시 입력을 공백 제거해 그대로 반환.
    """
    if not region:
        return ""
    # 시/군/구로 끝나는 토큰을 우선 (도(道)는 제외 — 광역 단위라 너무 넓음)
    for tok in region.split():
        m = re.fullmatch(r"([가-힣]+?)(특별자치시|광역시|특별시|시|군|구)", tok)
        if m:
            return m.group(1)
    m = _SIGUNGU_RE.search(region)
    if m and m.group(2) not in ("특별자치도",):
        return m.group(1)
    return region.replace(" ", "")


# ── 청크 스키마 ────────────────────────────────────────────────────────
class RegionalContextChunk(BaseModel):
    """RAG로 검색되는 지역 특색/워케이션 맥락 청크 (지역 × 단일 테마)."""
    region: str                       # 정규화 행정구역 (예: "강릉시")
    text: str                         # 1~3문장, 단일 테마
    facet: list[str] = []             # ["work"] | ["local"] | ["work","local"]
    admin_level: str = "sigungu"      # sido | sigungu | emd
    themes: list[str] = []
    vibe_tags: list[str] = []
    hobby_tags: list[str] = []        # tourism_hobby 매칭용
    season_peak: list[str] = []
    workation_fit: str = ""           # 원격근무 적합성 한 줄 (work facet)
    source: str = ""                  # "wikipedia" | "seed" 등
    source_date: str = ""             # 신선도 관리
    score: Optional[float] = None     # 검색 유사도 (1 - cosine distance)


# ── Chroma 컬렉션 (지연 초기화, 실패해도 앱을 죽이지 않음) ──────────────
_collection_obj = None  # 캐시


def _get_collection():
    """Chroma 컬렉션(영속) 반환. 실패 시 None — RAG는 옵션이므로 조용히 비활성."""
    global _collection_obj
    if _collection_obj is not None:
        return _collection_obj
    try:
        import chromadb  # 지연 import: 미설치여도 모듈 로드는 깨지지 않게
        client = chromadb.PersistentClient(path=RAG_CHROMA_DIR)
        _collection_obj = client.get_or_create_collection(
            name=RAG_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        return _collection_obj
    except Exception as e:
        logger.warning("RAG Chroma 초기화 실패 — RAG 비활성으로 진행: %s", e)
        return None


def _chunk_id(c: RegionalContextChunk) -> str:
    """안정 ID — 같은 (지역·출처·본문)이면 재실행에도 동일 → upsert가 중복 방지."""
    raw = f"{c.region}|{c.source}|{c.text}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _metadata(c: RegionalContextChunk) -> dict:
    """Chroma 메타데이터(스칼라만). 리스트 필드는 chunk_json으로 복원."""
    return {
        "region": c.region,
        "admin_level": c.admin_level,
        "facet_work": "work" in c.facet,
        "facet_local": "local" in c.facet,
        "source": c.source or "",
        "source_date": c.source_date or "",
        "chunk_json": c.model_dump_json(),
    }


# ── 쓰기: 빌더가 사용 ──────────────────────────────────────────────────
async def upsert_chunks(chunks: list[RegionalContextChunk]) -> int:
    """청크를 임베딩해 idempotent upsert. 반환: 처리 건수 (실패 시 0)."""
    col = _get_collection()
    if col is None or not chunks:
        return 0
    texts = [c.text for c in chunks]
    embeddings = await get_embeddings(texts)
    col.upsert(
        ids=[_chunk_id(c) for c in chunks],
        embeddings=embeddings,
        documents=texts,
        metadatas=[_metadata(c) for c in chunks],
    )
    return len(chunks)


def delete_region_source(region: str, source: str) -> None:
    """특정 (지역·출처) 청크를 모두 삭제. 빌더가 재적재 전에 호출.

    seed는 본문이 결정적이라 upsert만으로 깨끗하지만, wikipedia 청크는 LLM 생성이라
    재빌드 시 본문(→ID)이 달라질 수 있어 이전 청크가 고아로 남는다. 출처 단위로
    먼저 지운 뒤 새로 넣으면 재빌드가 멱등해진다.
    """
    col = _get_collection()
    if col is None:
        return
    try:
        col.delete(where={"$and": [{"region": region}, {"source": source}]})
    except Exception as e:
        logger.warning("RAG 삭제 실패 (region=%s, source=%s): %s", region, source, e)


def delete_source(source: str) -> None:
    """특정 출처(source)의 청크를 지역 구분 없이 모두 삭제.

    멀티지역 URL은 한 페이지가 여러 지역 청크를 만들므로, 재적재 전 source 단위로
    전부 지워야 교체가 깨끗하다.
    """
    col = _get_collection()
    if col is None:
        return
    try:
        col.delete(where={"source": source})
    except Exception as e:
        logger.warning("RAG 출처 삭제 실패 (source=%s): %s", source, e)


# ── 읽기: 에이전트가 사용 ──────────────────────────────────────────────
async def _retrieve(
    region: str,
    user_hints: list[str],
    facet: str,
    top_k: int,
) -> list[RegionalContextChunk]:
    """facet 필터 + 시맨틱 검색. 어떤 오류에도 [] 반환 (RAG는 옵션)."""
    try:
        col = _get_collection()
        if col is None or col.count() == 0:
            return []

        # region을 쿼리에 함께 넣어 같은 지역 청크가 상위로 오게 한다.
        # (TODO: region 메타 필터로 정밀화 — 현재 파일럿은 facet 필터 + region 쿼리 가중)
        parts = [region] + [h for h in (user_hints or []) if h and h.strip()]
        query = " ".join(p for p in parts if p and p.strip()).strip()
        if not query:
            return []

        emb = (await get_embeddings([query]))[0]
        flag = "facet_work" if facet == "work" else "facet_local"
        # facet으로 1차 필터한 뒤, 지역은 Python에서 후필터(메타 포맷 차이 흡수).
        # 후필터로 줄어들 것에 대비해 넉넉히 가져온다.
        res = col.query(
            query_embeddings=[emb],
            n_results=max(top_k * 4, 20),
            where={flag: True},
        )

        req_core = _sigungu_core(region)
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[RegionalContextChunk] = []
        for m, d in zip(metas, dists):
            try:
                chunk = RegionalContextChunk.model_validate_json(m["chunk_json"])
            except Exception:
                continue
            # 요청 지역이 식별되면 그 지역 청크만 — 타 지역 맥락 혼입(bleed) 차단.
            if req_core and _sigungu_core(chunk.region) != req_core:
                continue
            chunk.score = round(1.0 - d, 4) if d is not None else None
            out.append(chunk)
            if len(out) >= top_k:
                break
        return out
    except Exception as e:
        logger.warning("RAG 검색 실패 (facet=%s, region=%s): %s", facet, region, e)
        return []


async def retrieve_regional_context(
    region: str,
    user_hints: list[str],
    top_k: int = 5,
) -> list[RegionalContextChunk]:
    """지역 경험(local) 맥락 검색 — 정체성·명소·분위기·계절·취미.

    user_hints: [tourism_hobby, desired_vibe, region_style] 등.
    """
    return await _retrieve(region, user_hints, "local", top_k)


async def retrieve_work_context(
    region: str,
    user_hints: list[str],
    top_k: int = 5,
) -> list[RegionalContextChunk]:
    """워케이션·원격근무(work) 맥락 검색 — 코워킹·작업 카페 문화·장기체류 여건.

    user_hints: [work_style, "워케이션 원격근무" 등].
    """
    return await _retrieve(region, user_hints, "work", top_k)
