from __future__ import annotations

import json
import re
from typing import Optional, Type, TypeVar, Union

# ── LLM 프로바이더 설정 ──────────────────────────────────────────────
# 현재: OpenAI 사용 (Anthropic 크레딧 소진 시)
# 전환 방법: 아래 주석 해제 후 OpenAI 관련 줄 주석 처리
#
# [Anthropic으로 전환 시]
# from langchain_anthropic import ChatAnthropic
# from app.config.settings import ANTHROPIC_API_KEY, LLM_MODEL, ...
# _llm = ChatAnthropic(model="claude-opus-4-8", api_key=ANTHROPIC_API_KEY, max_tokens=4096)

from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config.settings import (
    LLM_MODEL,
    OPENAI_API_KEY,
    OPENAI_EMBEDDING_MODEL,
)

T = TypeVar("T", bound=BaseModel)

_llm = ChatOpenAI(model=LLM_MODEL, api_key=OPENAI_API_KEY, max_tokens=4096)
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
    """공용 OpenAI 호출."""
    from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

    llm = _llm
    if model and model != LLM_MODEL:
        llm = ChatOpenAI(model=model, api_key=OPENAI_API_KEY, max_tokens=max_tokens)

    lc_messages = []
    if system:
        lc_messages.append(SystemMessage(content=system))
    for msg in messages:
        if msg["role"] == "user":
            lc_messages.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
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


# ── Tool Use 루프 (OpenAI function calling) ───────────────────────────
async def call_llm_with_tools(
    messages: list[dict],
    tools: list[dict],
    tool_executor,
    system: Optional[str] = None,
    max_rounds: int = 3,
    max_tokens: int = 2048,
) -> list:
    """Tool use 루프 — OpenAI function calling 사용.

    tools 형식 (Anthropic input_schema → OpenAI parameters 자동 변환):
        [{"name": "...", "description": "...", "input_schema": {...}}]
    """
    # Anthropic 형식 → OpenAI 형식 변환
    oai_tools = []
    for t in tools:
        oai_tools.append({
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t.get("description", ""),
                "parameters":  t.get("input_schema", t.get("parameters", {})),
            }
        })

    msgs = list(messages)
    if system:
        msgs = [{"role": "system", "content": system}] + msgs

    collected: list = []
    for _ in range(max_rounds):
        resp = await _openai.chat.completions.create(
            model=LLM_MODEL,
            messages=msgs,
            tools=oai_tools,
            max_completion_tokens=max_tokens,
            parallel_tool_calls=True,
        )
        choice = resp.choices[0]
        msgs.append(choice.message.model_dump(exclude_unset=False))

        if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
            break

        # 한 라운드에 여러 tool_call이 있으면 병렬 실행
        async def _exec(tc):
            args = json.loads(tc.function.arguments)
            summary, payload = await tool_executor(tc.function.name, args)
            return tc.id, summary, payload

        import asyncio as _asyncio
        raw = await _asyncio.gather(*[_exec(tc) for tc in choice.message.tool_calls])

        tool_results = []
        for tc_id, summary, payload in raw:
            collected.append(payload)
            tool_results.append({
                "role":         "tool",
                "tool_call_id": tc_id,
                "content":      summary,
            })
        msgs.extend(tool_results)

    return collected


# ── 임베딩 ────────────────────────────────────────────────────────────
async def get_embeddings(texts: list[str]) -> list[list[float]]:
    response = await _openai.embeddings.create(
        model=OPENAI_EMBEDDING_MODEL,
        input=texts,
    )
    return [item.embedding for item in response.data]
