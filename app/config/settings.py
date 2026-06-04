import os
from dotenv import load_dotenv

load_dotenv()

# ── Anthropic (tool use 전용으로만 유지) ──────────────────────
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]

# ── OpenAI (메인 LLM) ─────────────────────────────────────────
LLM_MODEL: str = os.environ["OPENAI_MODEL"]
LLM_MODEL_SONNET: str = os.environ.get("OPENAI_MODEL_MINI", os.environ["OPENAI_MODEL"])

# ── OpenAI ────────────────────────────────────────────────────
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL: str = os.environ["OPENAI_MODEL"]
OPENAI_EMBEDDING_MODEL: str = os.environ["OPENAI_EMBEDDING_MODEL"]

# ── KTO (한국관광공사) ─────────────────────────────────────────
KTO_API_KEY: str = os.environ["KTO_API_KEY"]
KTO_BASE_URL: str = "https://apis.data.go.kr/B551011/KorService2"

# ── Kakao ─────────────────────────────────────────────────────
KAKAO_REST_API_KEY: str = os.environ["KAKAO_REST_API_KEY"]
KAKAO_LOCAL_URL: str = "https://dapi.kakao.com/v2/local"
KAKAO_MOBILITY_URL: str = "https://apis-navi.kakaomobility.com/v1/directions"

# ── Naver ─────────────────────────────────────────────────────
NAVER_CLIENT_ID: str = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET: str = os.environ["NAVER_CLIENT_SECRET"]
NAVER_SEARCH_URL: str = "https://openapi.naver.com/v1/search/blog"

# ── 점수 구간 ─────────────────────────────────────────────────
SCORE_VERY_GOOD: int = 85    # 매우 적합
SCORE_GOOD: int = 70         # 적합
SCORE_CONDITIONAL: int = 55  # 조건부 적합
SCORE_LOW: int = 40          # 낮은 적합
                             # 0~39: 부적합

# ── Confidence 구간 ───────────────────────────────────────────
CONFIDENCE_VERY_HIGH: int = 85   # 근거 충분, 판단 매우 안정적
CONFIDENCE_HIGH: int = 70        # 주요 근거 충분, 신뢰도 높음
CONFIDENCE_MEDIUM: int = 55      # 기본 근거 있으나 일부 확인 필요
CONFIDENCE_LOW: int = 40         # 근거 제한적, 추론 비중 큼
                                 # 0~39: 정보 부족, 판단 신뢰도 낮음

# ── 검색 반경 (미터 단위, Living Agent 사용) ─────────────────
SEARCH_RADIUS_WALK_M: int = 1500          # 도보 기본 반경
SEARCH_RADIUS_LOCAL_M: int = 2000         # 로컬 경험 반경
SEARCH_RADIUS_CAR_M: int = 15000          # 자동차 초기 반경 (15km)

# ── 검색 반경 (KM 단위, Work/Local Agent 사용) ───────────────
SEARCH_RADIUS_WALK_KM: float = SEARCH_RADIUS_WALK_M / 1000       # 1.5km
SEARCH_RADIUS_LOCAL_KM: float = SEARCH_RADIUS_LOCAL_M / 1000     # 2.0km
SEARCH_RADIUS_CAR_KM: float = 10.0                                # 자차 반경 (km)
SEARCH_RADIUS_CAR_SPEED_KMH: float = 40.0                        # 운전 속도 기준
SEARCH_RADIUS_CAR_MIN: int = 60                                    # 자동차 기준 60분

# ── 재호출 반경 확장 ──────────────────────────────────────────
RETRY_RADIUS_EXPAND_M: int = 1000                                  # 도보 확장 (+1km)
RETRY_RADIUS_EXPAND_KM: float = RETRY_RADIUS_EXPAND_M / 1000     # 1.0km
RETRY_CAR_EXPAND_M: int = 5000                                     # 자동차 확장 (+5km)
RETRY_CAR_EXPAND_MIN: int = 30                                     # 자동차 확장 30분

# ── 재호출 기준 ───────────────────────────────────────────────
RETRY_CONFIDENCE_THRESHOLD: int = 54    # confidence 54 이하면 재호출
RETRY_RESULT_EMPTY: bool = True         # 결과가 0개면 재호출
RETRY_MAX_COUNT: int = 1                # Agent당 최대 재호출 횟수
