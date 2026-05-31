from __future__ import annotations

from typing import Optional, Type, TypeVar, Union

import anthropic
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config.settings import (
    ANTHROPIC_API_KEY,
    LLM_MODEL,
    OPENAI_API_KEY,
    OPENAI_EMBEDDING_MODEL,
)

# Pydantic BaseModel을 상속한 클래스만 T로 사용할 수 있도록 제한
# 예: WorkEvaluation, LivingEvaluation, LocalEvaluation 등
T = TypeVar("T", bound=BaseModel)

# ── Clients ───────────────────────────────────────────────────────────────────

# Anthropic 비동기 클라이언트 — Claude 호출 전용
# 모듈 로드 시 한 번만 생성하여 재사용 (커넥션 풀 유지)
_anthropic = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# OpenAI 비동기 클라이언트 — 임베딩 전용
# Claude는 임베딩 API를 제공하지 않으므로 OpenAI를 별도로 사용
_openai = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def call_llm(
    messages: list[dict],
    system: Optional[str] = None,
    output_schema: Optional[Type[T]] = None,
    max_tokens: int = 4096,
    use_cache: bool = True,
    model: Optional[str] = None,
) -> Union[str, T]:
    """
    에이전트 공용 Claude 호출 함수.

    Args:
        messages:       [{"role": "user", "content": "..."}] 형식의 대화 목록
        system:         시스템 프롬프트 (에이전트별 역할 지시문)
        output_schema:  반환받을 Pydantic 모델 클래스.
                        넘기면 structured output으로 파싱된 인스턴스를 반환,
                        None이면 텍스트(str)를 반환.
        max_tokens:     Claude가 생성할 최대 토큰 수
        use_cache:      True면 system prompt에 prompt caching 적용.
                        동일한 시스템 프롬프트 반복 호출 시 비용 절감 (5분 TTL).

    Returns:
        output_schema가 있으면 해당 Pydantic 인스턴스, 없으면 str.
    """
    # 모델명, 토큰 한도, 메시지를 기본 파라미터로 묶음
    create_kwargs: dict = dict(
        model=model or LLM_MODEL,
        max_tokens=max_tokens,
        messages=messages,
    )

    # 시스템 프롬프트가 있을 때만 system 파라미터 추가
    if system:
        system_block: dict = {"type": "text", "text": system}
        if use_cache:
            # cache_control을 붙이면 Anthropic 서버가 이 블록을 캐싱함.
            # 동일한 시스템 프롬프트로 반복 호출 시 입력 토큰 비용 ~90% 절감.
            system_block["cache_control"] = {"type": "ephemeral"}
        create_kwargs["system"] = [system_block]

    # output_schema가 지정된 경우: structured output 모드
    if output_schema is not None:
        # messages.parse()는 Claude 응답을 output_schema Pydantic 모델로 자동 파싱.
        # 응답이 스키마와 맞지 않으면 SDK가 재시도함.
        response = await _anthropic.messages.parse(
            output_format=output_schema,
            **create_kwargs,
        )
        # parsed_output: output_schema 인스턴스 (예: WorkEvaluation)
        return response.parsed_output  # type: ignore[return-value]

    # output_schema가 없는 경우: 일반 텍스트 응답
    response = await _anthropic.messages.create(**create_kwargs)
    # Claude 응답은 content 리스트로 옴. 첫 번째 text 블록을 꺼내 반환.
    # text 블록이 없으면 빈 문자열 반환.
    return next(
        (block.text for block in response.content if block.type == "text"), ""
    )


async def get_embeddings(texts: list[str]) -> list[list[float]]:
    """
    텍스트 목록을 벡터로 변환 (RAG의 인덱싱 / 검색 단계에서 사용).

    Args:
        texts: 벡터로 변환할 텍스트 목록

    Returns:
        각 텍스트에 대응하는 float 벡터 목록.
        벡터 DB(Chroma, Pinecone 등)에 저장하거나 유사도 계산에 활용.
    """
    response = await _openai.embeddings.create(
        model=OPENAI_EMBEDDING_MODEL,
        input=texts,
    )
    # response.data는 임베딩 객체 리스트 — .embedding 속성이 실제 벡터(float 리스트)
    return [item.embedding for item in response.data]
