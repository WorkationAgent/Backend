"""
run_work_agent.py — Work Agent 단독 테스트

더미 숙소 데이터 + 실제 카카오/네이버 API 연동
실행: ./.venv/Scripts/python.exe run_work_agent.py
"""

import os
import sys
import json

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from app.schemas.user_input import UserInput
from app.agents.work_agent import work_agent

# ── 사용자 입력 ───────────────────────────────────────────────────
user_input = UserInput(
    purpose="워케이션",
    work_required=True,
    work_style="노트북으로 코딩, 가끔 화상회의",
    transport="도보 위주 뚜벅이",
    budget="카페 음료값 정도는 가능",
    companion="반려견 동반",
    desired_vibe="조용하고 집중되는 공간",
    additional_request="와이파이는 꼭 빵빵해야 함",
)

# ── 더미 숙소 (Stay Agent 결과 대체) ─────────────────────────────
candidate_accommodations = [
    {
        "id": "1234567",
        "rank": 1,
        "name": "제주 바다뷰 독채 펜션",
        "address": "제주특별자치도 제주시 구좌읍",
        "total_score": 88.5,
        "mapx": 126.8500,
        "mapy": 33.5563,
    },
    {
        "id": "2345678",
        "rank": 2,
        "name": "조용한 시골 한옥 스테이",
        "address": "제주특별자치도 서귀포시",
        "total_score": 81.0,
        "mapx": 126.5600,
        "mapy": 33.2541,
    },
    {
        "id": "3456789",
        "rank": 3,
        "name": "좌표 없는 숙소 (예외 처리 확인용)",
        "address": "주소 정보 미상",
        "total_score": 75.0,
        "mapx": None,
        "mapy": None,
    },
]

# ── 실행 ──────────────────────────────────────────────────────────
state = {
    "user_input": user_input,
    "candidate_accommodations": candidate_accommodations,
    "selected_region": {"region_name": "제주 구좌읍 세화리 생활권"},
}

result = work_agent(state)

# ── 출력 ──────────────────────────────────────────────────────────
print("=" * 70)
print(" Work Agent 결과")
print("=" * 70)

for ev in result["work_evaluations"]:
    d = ev.details
    acc_name = next(
        (a["name"] for a in candidate_accommodations if str(a["id"]) == ev.accommodation_id),
        ev.accommodation_id,
    )
    print(f"\n┌─ [{d.get('grade')}] {acc_name}")
    print(f"│  상태    : {d.get('status')}")
    print(f"│  점수    : {ev.score}점  |  신뢰도: {ev.confidence}")
    print(f"│  거리    : {d.get('distance')}  |  장소 수: {d.get('workplace_count')}곳")
    print(f"│  장점    : {', '.join(d.get('environment', [])) or '없음'}")
    print(f"│  리스크  : {', '.join(d.get('risks', [])) or '없음'}")
    print(f"│  요약    : {ev.summary}")
    print("└" + "─" * 60)

if result.get("warnings"):
    print("\n[warnings]")
    for w in result["warnings"]:
        print(f"  - {w}")

print("\n" + "=" * 70)
print(" 전체 JSON")
print("=" * 70)
print(json.dumps(
    [ev.model_dump() for ev in result["work_evaluations"]],
    ensure_ascii=False, indent=2,
))
