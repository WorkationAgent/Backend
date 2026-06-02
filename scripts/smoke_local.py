# Backend/scripts/smoke_local.py
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.schemas.user_input import UserInput
from app.agents.local_agent import evaluate_accommodations


async def smoke():
    user_input = UserInput(
        purpose="조용한 곳에서 일하면서 바다 보고 싶음",
        duration="3박 4일",
        desired_vibe="감성적이고 한적한",
        tourism_hobby="커피, 산책",
        transport="자동차",
    )
    accommodations = [
        {
            "id": "TEST001",
            "name": "강릉 테스트 숙소",
            "latitude": 37.7519,
            "longitude": 128.8761,
            "address": "강원 강릉시 안목해변길 100",
            "region": "강원 강릉",
        },
    ]
    results = await evaluate_accommodations(
        accommodations, user_input, stay_dates=("20260615", "20260618")
    )
    for r in results:
        print(r.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(smoke())