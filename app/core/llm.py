from __future__ import annotations

import json
import re
from typing import Optional, Type, TypeVar, Union

import anthropic
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config.settings import (
    ANTHROPIC_API_KEY,
    LLM_MODEL,
    OPENAI_API_KEY,
    OPENAI_EMBEDDING_MODEL,
)

T = TypeVar("T", bound=BaseModel)

_anthropic = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
_openai = AsyncOpenAI(api_key=OPENAI_API_KEY)


# ── JSON 추출 헬퍼 ───────────────────────────────────────────────────
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> str:
    """LLM 응답에서 JSON 본문만 추출.
```json 펜스나 앞뒤 설명 텍스트가 있어도 대응.
    """
    text = _FENCE_RE.sub("", text.strip()).strip()
    # 첫 { 또는 [부터 시작
    candidates = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if candidates:
        text = text[min(candidates):]
    # 마지막 } 또는 ]에서 끝
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
    max_retries: int = 1,
) -> Union[str, T]:
    """공용 Claude 호출.
    
    output_schema가 지정되면 JSON 모드로 호출하고 수동 파싱.
    (구조화 출력의 'Schema too complex' 회피용.)
    """
    create_kwargs: dict = dict(
        model=LLM_MODEL,
        max_tokens=max_tokens,
        messages=list(messages),
    )

    if system:
        system_block: dict = {"type": "text", "text": system}
        if use_cache:
            system_block["cache_control"] = {"type": "ephemeral"}
        create_kwargs["system"] = [system_block]

    # 일반 텍스트 응답
    if output_schema is None:
        response = await _anthropic.messages.create(**create_kwargs)
        return next(
            (b.text for b in response.content if b.type == "text"), ""
        )

    # 구조화 응답: JSON 스키마 안내 + 수동 파싱
    schema_str = json.dumps(
        output_schema.model_json_schema(), ensure_ascii=False
    )
    augmented = list(create_kwargs["messages"])
    last = augmented[-1]
    augmented[-1] = {
        **last,
        "content": (
            f"{last['content']}\n\n"
            f"---\n"
            f"**출력 형식**: 아래 JSON 스키마를 정확히 따르는 JSON만 출력.\n"
            f"마크다운 코드블록(```), 설명, 부가 텍스트 절대 금지.\n\n"
            f"스키마:\n{schema_str}"
        ),
    }
    create_kwargs["messages"] = augmented

    last_text = ""
    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            response = await _anthropic.messages.create(**create_kwargs)
            last_text = next(
                (b.text for b in response.content if b.type == "text"), ""
            )
            return output_schema.model_validate_json(_extract_json(last_text))
        except (ValidationError, json.JSONDecodeError) as e:
            last_error = e
            if attempt < max_retries:
                # 실패한 응답을 보여주고 다시 시도
                create_kwargs["messages"] = augmented + [
                    {"role": "assistant", "content": last_text},
                    {
                        "role": "user",
                        "content": (
                            "위 응답이 유효한 JSON이 아니어서 파싱 실패. "
                            "오직 JSON만 다시 출력해주세요."
                        ),
                    },
                ]

    raise RuntimeError(
        f"LLM JSON 파싱 {max_retries + 1}회 실패: {last_error}\n"
        f"마지막 응답: {last_text[:500]}"
    )


# ── Tool Use 루프 (하이브리드 보강 단계용) ───────────────────────────
async def call_llm_with_tools(
    messages: list[dict],
    tools: list[dict],
    tool_executor,
    system: Optional[str] = None,
    max_rounds: int = 3,
    max_tokens: int = 2048,
) -> list:
    """Tool use 루프. LLM이 tool을 부르면 실행 결과를 다시 넣어 반복.
    max_rounds 도달하거나 LLM이 더 이상 tool을 부르지 않으면 종료.

    tool_executor(name, input) → (LLM에 보여줄 요약 str, 우리가 수집할 payload)
    반환: 수집된 payload 리스트 (각 tool_use 호출당 1개).
    """
    create_kwargs: dict = dict(
        model=LLM_MODEL,
        max_tokens=max_tokens,
        messages=list(messages),
        tools=tools,
    )
    if system:
        create_kwargs["system"] = [{"type": "text", "text": system}]

    collected: list = []
    for _ in range(max_rounds):
        resp = await _anthropic.messages.create(**create_kwargs)
        create_kwargs["messages"].append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                summary, payload = await tool_executor(block.name, block.input)
                collected.append(payload)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": summary,
                })
        create_kwargs["messages"].append({"role": "user", "content": tool_results})

    return collected


# ── 임베딩 (변경 없음) ────────────────────────────────────────────────
async def get_embeddings(texts: list[str]) -> list[list[float]]:
    response = await _openai.embeddings.create(
        model=OPENAI_EMBEDDING_MODEL,
        input=texts,
    )
    return [item.embedding for item in response.data]
