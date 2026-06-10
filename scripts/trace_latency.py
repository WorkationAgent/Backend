"""LangSmith 최근 run들의 노드별 latency를 조회한다.

지역 선택 전 단계(parse/interpret/region_search)가 실제로 느려졌는지
span 단위로 확인하기 위한 일회성 진단 스크립트.

사용:
    python scripts/trace_latency.py            # 최근 50개 run
    python scripts/trace_latency.py 120        # 최근 120개 run
"""
from __future__ import annotations

import sys
from collections import defaultdict

from dotenv import load_dotenv
from langsmith import Client

load_dotenv()

PROJECT = "Workation_Agent"

# 우리가 관심 있는 노드/스팬 이름 (LangGraph 노드명 + @traceable 이름)
PRE_SELECT = {
    "parse", "interpret", "region_search",
    "stay_region_search", "extract_region_insights",
}


def latency_s(run) -> float | None:
    if run.start_time and run.end_time:
        return (run.end_time - run.start_time).total_seconds()
    return None


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    client = Client()

    # API 페이지 한도(100)를 피하려고 제너레이터를 직접 잘라낸다.
    runs = []
    for r in client.list_runs(project_name=PROJECT):
        runs.append(r)
        if len(runs) >= limit:
            break
    if not runs:
        print(f"[!] 프로젝트 '{PROJECT}'에서 run을 찾지 못했습니다.")
        return

    print(f"=== 최근 run {len(runs)}개 (project={PROJECT}) ===\n")

    # 이름별 latency 모으기
    by_name: dict[str, list[float]] = defaultdict(list)
    rows: list[tuple] = []
    for r in runs:
        lat = latency_s(r)
        if lat is None:
            continue
        by_name[r.name].append(lat)
        rows.append((r.start_time, r.name, r.run_type, lat))

    # 1) 시간순 — 지역 선택 전 단계 스팬만
    print("── 시간순 (지역 선택 전 단계 스팬) ──")
    print(f"{'시작시각(UTC)':<26} {'스팬':<26} {'타입':<10} {'지연(초)':>8}")
    for start, name, rtype, lat in sorted(rows, key=lambda x: x[0] or 0):
        if any(k in name for k in PRE_SELECT):
            print(f"{str(start):<26} {name:<26} {str(rtype):<10} {lat:>8.2f}")

    # 2) 이름별 평균/최대/건수
    print("\n── 스팬 이름별 집계 (전체) ──")
    print(f"{'스팬':<30} {'건수':>5} {'평균(초)':>9} {'최대(초)':>9}")
    for name, lats in sorted(by_name.items(), key=lambda kv: -max(kv[1])):
        print(f"{name:<30} {len(lats):>5} {sum(lats)/len(lats):>9.2f} {max(lats):>9.2f}")


if __name__ == "__main__":
    main()
