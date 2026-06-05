# Planner 오케스트레이터-워커 구조 명세서

> 작성일: 2026-06-04  
> 대상 파일: `app/agents/planner_agent.py`, `app/graph/workflow.py`

---

## 1. 배경 및 문제 정의

### 1-1. 현재 구조

현재 `planner_phase2`는 사용자 입력과 무관하게 Work / Living / Local 세 워커를 항상 전부 실행한다.

```python
# 현재: 항상 셋 다 실행 (병렬화)
raw_results = await asyncio.gather(
    living_agent(living_state),
    _call_work(state, normalized),
    _call_local(state, normalized),
)
```

이는 **병렬화(Parallelization / Sectioning)** 패턴이다.  
서브태스크가 미리 고정되어 있어 입력과 무관하게 항상 동일하게 실행된다.

### 1-2. 문제점

| 문제 | 설명 |
|---|---|
| 불필요한 호출 | 촌캉스·휴식형 사용자에게도 Work Agent를 실행 → 비용·지연 낭비 |
| 아키텍처 불일치 | 오케스트레이터-워커라고 부를 수 없음 |
| 확장성 부족 | 새로운 워커 추가 시 항상 실행되는 구조 고착 |

### 1-3. 목표

Planner가 사용자 입력 해석 결과를 보고 **필요한 워커만 선택적으로 실행**한다.  
이를 통해 진정한 오케스트레이터-워커 구조를 달성한다.

> **오케스트레이터-워커 기준**: 서브태스크를 오케스트레이터가 입력을 보고 런타임에 동적으로 결정한다.  
> — Anthropic, *Building Effective Agents*

---

## 2. 워커 선택 규칙

### 2-1. 워커별 실행 조건

세 워커 모두 선택적으로 실행한다. Planner가 `interpret_user_input` 해석 결과를 기반으로 결정한다.

| 워커 | 실행 조건 | 스킵 조건 |
|---|---|---|
| **Work Agent** | `work_required = True` | `work_required = False` or `None` |
| **Living Agent** | `priority_weights["living"] > 0.05` | 생활 인프라 완전 불필요 시 (사실상 항상 실행) |
| **Local Agent** | `priority_weights["local"] > 0.05` | 관광·로컬 경험 완전 불필요 시 |

### 2-2. 입력 시나리오별 실행 예시

| 입력 | Work | Living | Local |
|---|---|---|---|
| "워케이션, 카페 작업, 관광도 하고 싶어" | ✅ | ✅ | ✅ |
| "촌캉스, 일은 안 해, 맛집 다니고 싶어" | ❌ | ✅ | ✅ |
| "일만 할 거야, 관광은 필요 없어" | ✅ | ✅ | ❌ |
| "그냥 바다 보며 쉬러 가요" | ❌ | ✅ | ❌ |

> **Living은 사실상 항상 실행된다.**  
> 어떤 여행 유형이든 생활 인프라(마트, 병원 등)는 필요하므로,  
> 구조상 선택적으로 설계하되 실질적으로 스킵되는 경우는 거의 없다.

### 2-3. 판단 로직 (의사코드)

```python
# Planner가 interpret_user_input 결과를 기반으로 판단
run_work   = getattr(user_input, "work_required", None) is True
run_living = priority_weights.get("living", 0) > 0.05
run_local  = priority_weights.get("local", 0) > 0.05

workers = []
if run_living: workers.append(living_agent(living_state))
if run_work:   workers.append(_call_work(state, normalized))
if run_local:  workers.append(_call_local(state, normalized))

results = await asyncio.gather(*workers, return_exceptions=True)
```

---

## 3. 재호출 정책

Planner 레벨의 재호출은 없다.  
재호출은 **각 서브 에이전트 내부**에서만 처리한다.

### 3-1. 서브 에이전트 재호출 기준 (통일)

| 트리거 | 동작 | 최대 횟수 |
|---|---|---|
| 결과 0개 | 반경 확장 후 재시도 | 1회 |
| `confidence ≤ 54` | 반경 확장 후 재시도 | 1회 |

### 3-2. 재시도 파라미터

| 항목 | 기본값 | 재시도 시 |
|---|---|---|
| 도보 반경 | 1.5km | +1.0km → 2.5km |
| 자동차 기준 시간 | 60분 | +30분 → 90분 |
| 로컬 경험 반경 | 2.0km | +1.0km → 3.0km |

### 3-3. 재시도 후에도 부족한 경우

재시도 후에도 결과가 부족하거나 confidence가 낮으면:
- `warnings`에 "확인 필요사항" 기록 후 종료
- 플래너는 이를 최종 출력에 포함해 사용자에게 안내

---

## 4. 목표 그래프 구조

```
planner_start
   └─ 줄글 파싱 · UserInput 구조화 · 5개 조건 해석 · priority_weights 산출
        ↓
stay_phase1 (지역 후보 3개 탐색)
        ↓
human_select  ← 사용자 지역 선택 (Human-in-the-loop)
        ↓
stay_phase2 (숙소 후보 3개 탐색)
        ↓
[동적 워커 선택]  ← priority_weights, work_required 기반
   ├─ living_agent  (사실상 항상)
   ├─ work_agent    (work_required=True 일 때만)
   └─ local_agent   (local 가중치 > 0.05 일 때만)
        ↓
planner_integrate
   └─ 종합 점수 계산 (코드 기반 가중 평균)
      ranked_recommendations 생성
        ↓
planner_finish → 최종 출력
```

---

## 5. 종합 점수 계산

LLM이 점수를 내는 방식에서 **코드 기반 가중 평균**으로 변경한다.

```python
def calculate_final_score(
    work_score: float | None,
    living_score: float | None,
    local_score: float | None,
    stay_score: float | None,
    priority_weights: dict,
) -> float:
    score = 0.0
    if work_score is not None:
        score += work_score * priority_weights.get("work", 0)
    if living_score is not None:
        score += living_score * priority_weights.get("living", 0)
    if local_score is not None:
        score += local_score * priority_weights.get("local", 0)
    if stay_score is not None:
        score += stay_score * priority_weights.get("accommodation", 0)
    return round(score, 1)
```

> `stay_score`는 숙소 스타일·감성·예산·동반자 적합성을 평가하며  
> `priority_weights["accommodation"]` 차원에 매핑된다.

---

## 6. 관련 state 필드

```python
class GraphState(TypedDict):
    # Planner 해석 결과 (워커 선택 기준)
    user_input: UserInput              # work_required 포함
    priority_weights: Dict[str, float] # work/living/local/accommodation/transport

    # 실행된 워커 추적
    retry_count: Dict[str, int]        # {"work": 0, "living": 1, ...}
    warnings: List[str]                # 확인 필요사항
```

---

## 7. 관련 상수 (settings.py)

| 상수 | 값 | 설명 |
|---|---|---|
| `RETRY_CONFIDENCE_THRESHOLD` | 54 | 이하이면 서브 에이전트 자체 재시도 |
| `RETRY_MAX_COUNT` | 1 | 층위별 최대 재시도 횟수 |
| `SEARCH_RADIUS_WALK_KM` | 1.5 | 도보 기본 반경 (km) |
| `SEARCH_RADIUS_LOCAL_KM` | 2.0 | 로컬 경험 기본 반경 (km) |
| `SEARCH_RADIUS_CAR_MIN` | 60 | 자동차 기준 이동시간 (분) |
| `RETRY_RADIUS_EXPAND_KM` | 1.0 | 재시도 시 반경 확장 (km) |
| `RETRY_CAR_EXPAND_MIN` | 30 | 재시도 시 자동차 시간 확장 (분) |

---

## 8. 구현 체크리스트

- [ ] `planner_phase2`: 동적 워커 선택 로직 추가
- [ ] `work_agent`: `must_have_conditions` state에서 읽도록 수정
- [ ] `living_agent`: `must_have_conditions` state에서 읽도록 수정
- [ ] `local_agent`: `must_have_conditions` state에서 읽도록 수정
- [ ] 각 서브 에이전트: 재호출 기준 통일 (결과 0개 or confidence ≤ 54)
- [ ] `build_final_output`: LLM 점수 산정 → 코드 기반 가중 평균으로 교체
- [ ] `workflow.py`: 새 그래프 구조 반영
