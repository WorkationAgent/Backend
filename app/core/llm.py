from __future__ import annotations

import json
import re
from typing import Optional, Type, TypeVar, Union

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config.settings import (
    ANTHROPIC_API_KEY,
    LLM_MODEL,
    OPENAI_API_KEY,
    OPENAI_EMBEDDING_MODEL,
)

T = TypeVar("T", bound=BaseModel)

_anthropic = ChatAnthropic(model=LLM_MODEL, api_key=ANTHROPIC_API_KEY, max_tokens=4096)
_openai = AsyncOpenAI(api_key=OPENAI_API_KEY)


# ── JSON 추출 헬퍼 ───────────────────────────────────────────────────
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> str:
    """LLM 응답에서 JSON 본문만 추출."""
    text = _FENCE_RE.sub("", text.strip()).strip()
    candidates = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if candidates:
        text = text[min(candidates):]
    end = max(text.rfind("}"), text.rfind("]"))
    if end >= 0:
        text = text[: end + 1]
    return text


# ── 공용 호출 함수 ───────────────────────────────────────────────────
async def call_llm(
    messages: list[dict],
    system: Optional[str] = None,
    output_schema: Optional[Type[T]] = None,
    max_tokens: int = 4096,
    use_cache: bool = True,
    model: Optional[str] = None,
    max_retries: int = 1,
) -> Union[str, T]:
    """공용 Claude 호출."""
    llm = _anthropic
    if model and model != LLM_MODEL:
        llm = ChatAnthropic(model=model, api_key=ANTHROPIC_API_KEY, max_tokens=max_tokens)

    lc_messages = []
    if system:
        lc_messages.append(SystemMessage(content=system))
    for msg in messages:
        if msg["role"] == "user":
            lc_messages.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            from langchain_core.messages import AIMessage
            lc_messages.append(AIMessage(content=msg["content"]))

    if output_schema is None:
        response = await llm.ainvoke(lc_messages)
        return response.content

    # 구조화 응답: JSON 스키마 안내 + 수동 파싱
    schema_str = json.dumps(output_schema.model_json_schema(), ensure_ascii=False)
    last_msg = lc_messages[-1]
    lc_messages[-1] = HumanMessage(
        content=(
            f"{last_msg.content}\n\n"
            f"---\n"
            f"**출력 형식**: 아래 JSON 스키마를 정확히 따르는 JSON만 출력.\n"
            f"마크다운 코드블록(```), 설명, 부가 텍스트 절대 금지.\n\n"
            f"스키마:\n{schema_str}"
        )
    )

    last_text = ""
    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            response = await llm.ainvoke(lc_messages)
            last_text = response.content
            return output_schema.model_validate_json(_extract_json(last_text))
        except (ValidationError, json.JSONDecodeError) as e:
            last_error = e
            if attempt < max_retries:
                from langchain_core.messages import AIMessage
                lc_messages = lc_messages + [
                    AIMessage(content=last_text),
                    HumanMessage(content="위 응답이 유효한 JSON이 아니어서 파싱 실패. 오직 JSON만 다시 출력해주세요."),
                ]

    raise RuntimeError(
        f"LLM JSON 파싱 {max_retries + 1}회 실패: {last_error}\n"
        f"마지막 응답: {last_text[:500]}"
    )


# ── 임베딩 ────────────────────────────────────────────────────────────
async def get_embeddings(texts: list[str]) -> list[list[float]]:
    response = await _openai.embeddings.create(
        model=OPENAI_EMBEDDING_MODEL,
        input=texts,
    )
    return [item.embedding for item in response.data]
