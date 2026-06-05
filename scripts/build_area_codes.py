"""KTO 지역 코드 테이블 생성 스크립트.

한 번만 실행하면 됨 (코드는 거의 변동 없음).
실행: cd Backend && python -m scripts.build_area_codes

출력: app/tools/area_codes.json
  {
    "<sido_code>": {
      "name": "전라북도",
      "sigungu": {"<sg_code>": "남원시", ...}
    },
    ...
  }
"""
import asyncio
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx

# 프로젝트 루트(Backend/)를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import KTO_API_KEY, KTO_BASE_URL


async def build():
    base_params = {
        "serviceKey": KTO_API_KEY,
        "MobileOS": "ETC",
        "MobileApp": "WorkationAgent",
        "_type": "json",
        "numOfRows": 100,
        "pageNo": 1,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 시도 목록
        r = await client.get(f"{KTO_BASE_URL}/areaCode2", params=base_params)
        data = r.json()
        sido_items = data["response"]["body"]["items"]["item"]
        if isinstance(sido_items, dict):
            sido_items = [sido_items]

        result = {}
        for sido in sido_items:
            code = str(sido["code"])
            # 시군구 목록
            r2 = await client.get(
                f"{KTO_BASE_URL}/areaCode2",
                params={**base_params, "areaCode": code},
            )
            data2 = r2.json()
            sg_raw = data2["response"]["body"]["items"].get("item", [])
            if isinstance(sg_raw, dict):
                sg_raw = [sg_raw]

            result[code] = {
                "name": sido["name"],
                "sigungu": {str(s["code"]): s["name"] for s in sg_raw},
            }
            print(f"  {sido['name']} ({code}): 시군구 {len(sg_raw)}개")

    out = Path(__file__).resolve().parent.parent / "app" / "tools" / "area_codes.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장 완료: {out}")
    print(f"시도 {len(result)}개")


if __name__ == "__main__":
    asyncio.run(build())
