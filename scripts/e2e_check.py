"""LangGraph 리팩토링 end-to-end 검증 — 실제 서버에 /plan → /select-region 호출."""
import asyncio
import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"

TEXT = (
    "제주도 쪽으로 한 달 살기 하려고 해요. 원격근무라 와이파이 빠르고 카페에서 "
    "일하기 좋은 곳이면 좋겠고, 바다 근처에 조용한 분위기였으면 해요. 차는 없어서 "
    "대중교통이나 도보로 다닐 수 있어야 하고, 예산은 한 달에 100만원 정도예요. "
    "혼자 가고 주변에 마트랑 병원 있으면 좋겠어요."
)


def line(c="─"):
    print(c * 70)


async def main():
    async with httpx.AsyncClient(timeout=600.0) as client:
        # ── STEP 1: /api/plan ────────────────────────────────────────────
        line("=")
        print("STEP 1  POST /api/plan  (parse → interpret → region_search → interrupt)")
        line("=")
        r = await client.post(f"{BASE}/api/plan", json={"text": TEXT})
        print("HTTP", r.status_code)
        plan = r.json()
        if r.status_code != 200:
            print("FAILED:", json.dumps(plan, ensure_ascii=False)); return

        thread_id = plan["thread_id"]
        print("thread_id  :", thread_id)
        print("must_have  :", plan["parsed"]["must_have"])
        print("preferences:", plan["parsed"]["preferences"])
        print("candidate_regions:")
        for reg in plan["candidate_regions"]:
            print(f"   - id={reg['id']:<12} name={reg['name']:<20} best={reg.get('is_best')}")
        if not plan["candidate_regions"]:
            print("NO REGIONS — stopping"); return

        chosen = plan["candidate_regions"][0]
        print(f"\n→ 선택: {chosen['id']} ({chosen['name']})")

        # ── STEP 2: /api/select-region ───────────────────────────────────
        line("=")
        print("STEP 2  POST /api/select-region  (resume → accommodation → fan-out → integrate)")
        line("=")
        r2 = await client.post(
            f"{BASE}/api/select-region",
            json={"thread_id": thread_id, "region_id": chosen["id"]},
        )
        print("HTTP", r2.status_code)
        rec = r2.json()
        if r2.status_code != 200:
            print("FAILED:", json.dumps(rec, ensure_ascii=False)); return

        print("recommended_region:", rec["recommended_region"])
        print("results_subtitle  :", rec["results_subtitle"])
        print("matched_conditions:", rec["matched_conditions"])
        print(f"candidates ({len(rec['candidates'])}곳):")
        line()
        for c in rec["candidates"]:
            print(f"[#{c['rank']}] {c['name']}  종합 {c['overall_score']}점")
            print(f"      주소: {c['address']}")
            cs = c["category_scores"]
            print(f"      점수: work={cs['work']} living={cs['living']} local={cs['local']}")
            for key in ("work", "living", "local"):
                sec = c["sections"].get(key, {})
                if sec.get("skipped"):
                    print(f"      [{key}] (스킵) {sec.get('skip_reason','')}")
                else:
                    items = sec.get("items", [])
                    names = ", ".join(i["name"] for i in items[:3]) if items else "-"
                    print(f"      [{key}] {sec.get('score')}점 · {sec.get('summary','')[:50]} · 항목: {names}")
            print(f"      map_points: {len(c.get('map_points', []))}개")
            line()

        # ── 검증 체크리스트 ──────────────────────────────────────────────
        print("VERIFICATION:")
        print("  ✓ /plan 200 + thread_id 발급 + 지역 후보")
        print("  ✓ /select-region 200 (그래프 resume → 끝까지 실행)")
        print(f"  ✓ 최종 추천 {len(rec['candidates'])}곳 종합점수 순 정렬:",
              [c["overall_score"] for c in rec["candidates"]],
              "(내림차순)" if rec["candidates"] == sorted(rec["candidates"], key=lambda x: -x["overall_score"]) else "(정렬 이상!)")


if __name__ == "__main__":
    asyncio.run(main())
