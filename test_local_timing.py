"""
test_local_timing.py — Local Agent 실행 시간 측정

시작 시각부터 Local Agent 결과가 나올 때까지의 wall-clock 시간을 잰다.
(실제 KTO/Kakao/Naver API + LLM + RAG 호출이 일어나므로 .env 키가 필요)

실행 (Backend/ 에서):
  ./.venv/Scripts/python.exe test_local_timing.py            # 숙소 전체(3개) 병렬 측정
  ./.venv/Scripts/python.exe test_local_timing.py 1          # 숙소 1개만 측정
  ./.venv/Scripts/python.exe test_local_timing.py --each     # 숙소별 개별 시간도 순차 측정

측정 대상:
  local_agent(state) 호출 → {"local_evaluations": [...]} 반환까지의 시간.
  (evaluate_accommodations가 숙소들을 asyncio.gather로 병렬 평가하므로,
   전체 시간 ≈ 가장 오래 걸린 숙소 1개 기준. 숙소별 실제 시간은 --each로 확인)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from app.agents.local_agent import evaluate_accommodations, local_agent
from app.schemas.user_input import UserInput

# ── 테스트 입력 ───────────────────────────────────────────────────
USER_INPUT = UserInput(
    purpose="워케이션",
    duration="7일",
    desired_region="제주",
    desired_vibe="조용하고 자연친화적인 바닷가",
    tourism_hobby="해변 산책, 감성 카페",
    transport="도보 위주",
    companion="반려견 동반",
    additional_request="관광지처럼 붐비지 않는 동네",
)

# 함덕/표선/애월 일대. local agent가 latitude/longitude를 직접 쓰므로 필수.
ACCOMMODATIONS = [
    {
        "id": "loc-1", "name": "라마다제주함덕호텔",
        "address": "제주특별자치도 제주시 조천읍 신북로 470",
        "region": "제주 제주시 조천읍", "latitude": 33.543, "longitude": 126.669,
    },
    {
        "id": "loc-2", "name": "아망뜨펜션(제주)",
        "address": "제주특별자치도 서귀포시 표선면 민속해안로 11",
        "region": "제주 서귀포시 표선면", "latitude": 33.3245, "longitude": 126.843,
    },
    {
        "id": "loc-3", "name": "샐리스제주호텔",
        "address": "제주특별자치도 제주시 애월읍 고내로 46",
        "region": "제주 제주시 애월읍", "latitude": 33.4715, "longitude": 126.306,
    },
]

STAY_DATES = ("20260701", "20260707")
MUST_HAVE = ["바다 근처", "조용한 환경", "반려견 동반"]


def _state(accs: list[dict]) -> dict:
    return {
        "candidate_accommodations": accs,
        "user_input": USER_INPUT,
        "parsed_preferences": {"stay_dates": STAY_DATES},
        "must_have_conditions": MUST_HAVE,
    }


def _fmt(sec: float) -> str:
    return f"{sec:.2f}s"


# ── 전체(병렬) 측정 ───────────────────────────────────────────────
async def run_total(n: int) -> None:
    accs = ACCOMMODATIONS[:n]
    print("\n" + "=" * 60)
    print(f"  Local Agent 전체 실행 시간 (숙소 {len(accs)}개, 병렬)")
    print("=" * 60)

    print(f"  시작: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
    start = time.perf_counter()
    result = await local_agent(_state(accs))
    elapsed = time.perf_counter() - start
    print(f"  종료: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")

    evals = result.get("local_evaluations", [])
    print(f"\n  ⏱  총 소요 시간 : {_fmt(elapsed)}")
    print(f"  📦 결과 개수    : {len(evals)}개  (병렬 → 총시간 ≈ 가장 느린 1개)")

    for ev in evals:
        d = ev.details
        sig = len(getattr(d, "signature_spots", []) or [])
        daily = len(getattr(d, "daily_spots", []) or [])
        summary = (ev.summary or "")[:70]
        print(
            f"\n  · {ev.accommodation_id}: score={ev.score} conf={ev.confidence} "
            f"sig={sig} daily={daily}"
        )
        print(f"    {summary}")


# ── 숙소별 개별(순차) 측정 ────────────────────────────────────────
async def run_each(n: int) -> None:
    print("\n" + "=" * 60)
    print("  숙소별 개별 실행 시간 (순차 — 1개씩)")
    print("=" * 60)
    for acc in ACCOMMODATIONS[:n]:
        start = time.perf_counter()
        evals = await evaluate_accommodations([acc], USER_INPUT, STAY_DATES)
        elapsed = time.perf_counter() - start
        ev = evals[0]
        print(f"  · {acc['name']:20s} : {_fmt(elapsed):>8}  (score={ev.score})")


async def main() -> None:
    args = sys.argv[1:]
    each = "--each" in args
    nums = [a for a in args if a.isdigit()]
    n = int(nums[0]) if nums else len(ACCOMMODATIONS)
    n = max(1, min(n, len(ACCOMMODATIONS)))

    await run_total(n)
    if each:
        await run_each(n)

    print("\n" + "=" * 60)
    print("  완료")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
