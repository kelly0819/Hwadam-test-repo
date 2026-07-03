# 리팩터링 전 상태 기록 (Pre-Refactor Snapshot)

> 작성일: 2026-07-02  
> 브랜치: `develop`  
> 마지막 커밋: `5249ae272edcf1f46f7ff04c6821dd254fc8f706` — "Update README.md"

---

## 1. 현재 아키텍처 개요

### 1-1. create_react_agent의 Tool 호출 흐름

진입점은 `POST /internal/agent/run` (`AI/api/internal/agent_router.py:157`)이다.
요청을 받으면 즉시 `202 Accepted`를 반환하고, `_run_agent()`를 FastAPI BackgroundTask로 등록한다.
백그라운드 코루틴은 `agent_graph.ainvoke(initial_state)` (`agent_router.py:130`)를 호출하면서 LangGraph의 ReAct 루프가 시작된다.

`agent_graph`는 `AI/graph/action_model.py:64`에서 `create_react_agent`로 생성된 그래프다.
LLM(Qwen-Plus)이 시스템 프롬프트에 따라 아래 순서로 tool을 하나씩 선택·호출한다.

```
1. run_discovery_agent   → 후보 상품(candidates) + intent_vector 생성, 진행률 25%
2. run_score_agent       → 후보별 0~100 점수 산출, 진행률 60%
3. run_alternative_agent → feature_vec 기반 유사 대체 상품 검색
4. run_collaborative_agent → 유사 피부 조건 사용자 협업 필터링
```

4개 tool 호출이 끝나면 LLM이 최종 JSON을 생성한다.
이 JSON을 `agent_router.py:132-148`에서 파싱한 뒤, LLM이 배열을 임의 수정했을 경우를 대비해
`_collaborative_store`, `_alternative_store`, `_score_store` 사이드채널에서 실제 tool 결과로 덮어쓴다.
마지막으로 `_write_result()`가 Supabase의 `recommendation_jobs` 테이블에 COMPLETED 상태와 함께 결과를 저장한다.

### 1-2. 시스템 프롬프트가 순서를 강제하는 방식

`AI/graph/action_model.py:22-62`에 정의된 `SYSTEM_PROMPT` 전문:

```
당신은 화장품 추천 에이전트입니다. 주어진 도구를 자율적으로 판단해 호출하세요.

## 사용 가능한 도구
- run_discovery_agent: 사용자 프로필과 RAG 컨텍스트로 후보 상품을 탐색합니다. candidates를 반환합니다.
- run_score_agent: 후보 상품의 예산·가격·리뷰·개인화 점수를 계산합니다.
- run_alternative_agent: 기준 상품과 유사한 대체 상품을 벡터 검색으로 찾습니다.
- run_collaborative_agent: 유사한 피부 조건의 사용자들이 선호한 상품을 추천합니다.

## 실행 순서 (반드시 이 순서를 지키세요)
1. run_discovery_agent → candidates 수집
2. run_score_agent (discovery candidates 전달)
3. run_alternative_agent (기준 상품 ID + candidates의 product_id 전체를 exclude_product_ids로 전달)
4. run_collaborative_agent (기준 상품 ID + candidates의 product_id 전체를 exclude_product_ids로 전달)

## 도구 의존 관계
- run_discovery_agent: 후보 상품(candidates)과 intent_vector를 생성합니다. 반드시 가장 먼저 실행하세요.
- run_score_agent: run_discovery_agent의 candidates가 필요합니다. discovery 완료 후 호출하세요.
- run_alternative_agent: run_discovery_agent 완료 후 실행하세요. ...
- run_collaborative_agent: run_discovery_agent 완료 후 실행하세요. ...

## 필수 규칙
1. 모든 도구 호출 시 컨텍스트의 "작업 ID" 값을 job_id 파라미터로 반드시 전달하세요.
2. run_alternative_agent와 run_collaborative_agent 호출 시 exclude_product_ids에 ...
3. 도구가 반환한 배열은 절대 수정하지 마세요. ...

## 최종 출력
모든 도구 호출이 끝나면 반드시 아래 JSON만 출력하라. 설명, 마크다운, 코드블록 없이 순수 JSON만.
{ "matchScore": ..., "matchLabel": ..., "aiReason": ..., "similarUserProducts": ..., "alternativeProducts": ... }
```

순서 강제는 순전히 **자연어 지시**에 의존한다.
"반드시 이 순서를 지키세요"라는 텍스트가 LLM의 next-action 선택을 유도하는 유일한 메커니즘이다.
코드 레벨의 순서 보장(edge, conditional router 등)은 없다.

### 1-3. 이 방식의 구조적 한계

**핵심 문제: ReAct 루프는 매 스텝마다 LLM을 한 번 호출해 tool 하나를 선택한다.**

`create_react_agent`의 내부 동작은 다음 패턴을 반복한다.

```
LLM call → tool 선택 → tool 실행 → 결과를 메시지에 추가 → LLM call → ...
```

이 구조의 결과로 다음 한계가 발생한다.

**① 강제 직렬 실행 (병렬화 불가)**

`run_score_agent`는 `run_discovery_agent`의 결과에 의존하지만,
`run_alternative_agent`와 `run_collaborative_agent`는 서로 독립적이다.
즉, step 3과 step 4는 동시에 실행 가능함에도 불구하고 LLM이 순서대로 한 번씩 선택하므로
실제 실행은 항상 `score → alternative → collaborative` 순서의 직렬이다.
`alternative`와 `collaborative`가 각각 1~2초씩 걸린다면 이 둘을 병렬화하는 것만으로도
전체 응답 시간을 최대 절반 가까이 줄일 수 있다.

**② LLM round-trip 오버헤드 × 4**

tool 4개를 호출하려면 LLM을 최소 4번 호출해야 한다 (tool 선택 × 4 + 최종 출력 × 1 = 5회).
각 round-trip은 네트워크 지연 + 토큰 생성 시간을 포함한다.
`create_react_agent`는 이 round-trip을 피할 방법을 제공하지 않는다.

**③ LLM이 순서를 어길 가능성**

자연어 프롬프트만으로 순서를 강제하므로, LLM이 hallucination으로 step 3을 건너뛰거나
candidates를 전달하지 않고 `run_score_agent`를 호출하는 상황을 코드로 방어하기 어렵다.
현재 구현에서는 이를 사이드채널(`_intent_store`, `_score_store` 등)로 우회하고 있으며,
`agent_router.py:137-148`에서 LLM 출력을 실제 tool 결과로 덮어쓰는 패치가 필요한 이유가 바로 이것이다.

---

## 2. 각 Tool 함수 현황

### Tool 1 — Discovery (`run_discovery_agent`)

| 항목 | 내용 |
|---|---|
| 파일 | `AI/agents/tools.py:37-75` (tool 래퍼) |
| 구현 | `AI/agents/discovery_agent.py:30` (`run_discovery`) |
| async 여부 | **async** (tool 래퍼·구현 모두) |
| 외부 API 호출 | 없음 (LLM 호출 없는 순수 임베딩+벡터 검색) |
| 내부 흐름 | bge-m3 임베딩(로컬) → Supabase pgvector RPC(`match_products`, `match_user_contexts`) |

Discovery는 LLM을 호출하지 않는다. 연산 비용의 대부분은 bge-m3 임베딩(`asyncio.to_thread`로 오프로드)과
Supabase RPC 2회(RAG 컨텍스트 검색, 후보 상품 검색)다.
intent_vector(1024차원)는 LLM 메시지에 싣지 않고 `_intent_store[job_id]`에 저장하며,
`run_score_agent`가 pop으로 읽어간다 (`tools.py:96`).

### Tool 2 — Score (`run_score_agent`)

| 항목 | 내용 |
|---|---|
| 파일 | `AI/agents/tools.py:78-124` (tool 래퍼) |
| 구현 | `AI/agents/score_agent/__init__.py:44` (`run_score`) |
| async 여부 | **async** (tool 래퍼·구현 모두) |
| 외부 API 호출 | **DashScope** (가중치 조정용 Qwen-Plus 호출 1회) |

Score는 4개 서브 스코어러를 조합한다.

- `budget_scorer.compute()` — **sync**, `AI/agents/score_agent/budget_scorer.py`
- `price_scorer.compute()` — **sync**, `AI/agents/score_agent/price_scorer.py`
- `review_scorer.compute()` — **sync** (단, `asyncio.to_thread`로 감쌈), `AI/agents/score_agent/review_scorer.py`
- `personalization_scorer.compute()` — **sync**, `AI/agents/score_agent/personalization_scorer.py`

4개 스코어러 모두 동기 함수이며, 현재 구현에서는 후보(candidate_ids)를 **for 루프로 순차 처리**한다 (`score_agent/__init__.py:87-119`).
후보가 20개라면 20번 루프를 돌며 review_scorer의 Supabase 벡터 검색이 20회 순차 실행된다.
Qwen-Plus 가중치 조정 호출은 루프 전에 1회만 일어난다.

### Tool 3-① Alternative (`run_alternative_agent`)

| 항목 | 내용 |
|---|---|
| 파일 | `AI/agents/tools.py:127-152` (tool 래퍼) |
| 구현 | `AI/agents/alternative_agent.py:20` (`run_alternative`) |
| async 여부 | **async** (tool 래퍼·구현 모두) |
| 외부 API 호출 | 없음 (LLM 호출 없는 순수 벡터 검색) |
| 내부 흐름 | Supabase RPC `match_alternatives` → 상품 메타 배치 조회 |

`run_alternative`는 기준 상품의 feature_vec(DB에 사전 저장)을 기준으로
`match_alternatives` RPC를 호출한 뒤, 결과 상품들의 메타를 `get_products_meta`로 배치 조회한다.
LLM 호출이 전혀 없어 순수 I/O 비용만 발생한다.

### Tool 3-② Collaborative (`run_collaborative_agent`)

| 항목 | 내용 |
|---|---|
| 파일 | `AI/agents/tools.py:155-181` (tool 래퍼) |
| 구현 | `AI/agents/collaborative_agent/__init__.py:52` (`run_collaborative`) |
| async 여부 | **async** (tool 래퍼·구현 모두) |
| 외부 API 호출 | 없음 (LLM 호출 없음, Supabase SQL만) |
| 폴백 | `AI/agents/collaborative_agent/fallback.py` (`run_fallback`) |

`run_collaborative`는 피부 타입·퍼스널컬러가 일치하는 유사 사용자를 조회한 뒤,
그들의 user_products를 `weighted_score = Σ(rating × usage_weight)`로 집계한다.
유사 사용자가 없거나 집계 결과가 `MIN_VALID_RESULTS(=3)` 미만이면 fallback으로 전환한다.
LLM 호출이 없으며 Supabase 쿼리 2회(유사 사용자 조회, 제품 인터랙션 조회)가 전부다.

> **요약**: Tool 3-① Alternative와 Tool 3-② Collaborative는 서로 의존성이 없고 둘 다 LLM을 호출하지 않는다.
> 현재는 ReAct 루프가 이 둘을 순차 실행하지만, 이론적으로는 병렬 실행이 가능한 구조다.

---

## 3. 외부 API 호출 클라이언트 현황

### 3-1. DashScope (Qwen-Plus) 호출 방식

파일: `AI/services/qwen_client.py`

`QwenLLMClient`는 **동기 OpenAI SDK**(`from openai import OpenAI`)를 내부 클라이언트로 사용한다.
실제 HTTP 요청은 `_do_call()` 내부의 `self._client.chat.completions.create()`로 발생하며,
이 동기 함수를 `asyncio.to_thread(_do_call)`으로 감싸 async 인터페이스로 노출한다 (`qwen_client.py:66`).

즉, **async 클라이언트가 아니라 sync 클라이언트를 스레드풀에서 실행**하는 방식이다.
`AsyncOpenAI`를 사용하면 이벤트 루프를 직접 활용해 스레드 컨텍스트 스위칭 비용을 제거할 수 있다.

`ChatOpenAI`(LangChain 래퍼)는 `action_model.py:15`에서 오케스트레이터 LLM으로 별도 사용되며,
이쪽은 LangChain이 내부적으로 async를 처리한다.

### 3-2. Supabase 커넥션 방식

파일: `AI/db/supabase_client.py`

```python
_client = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_SERVICE_KEY,
    options=ClientOptions(
        httpx_client=httpx.Client(http2=False),  # HTTP/1.1 강제
    ),
)
```

- **Session Pooler 사용 여부**: 코드에 명시 없음. `SUPABASE_URL`이 Session Pooler 엔드포인트를 가리키는지는 `.env` 값에 달려 있다.
- **커넥션 풀 크기**: `httpx.Client` 기본값 사용 (httpx 기본 max_connections=100, max_keepalive_connections=20).
  풀 크기를 명시적으로 지정하지 않았다.
- **HTTP/2 비활성화**: stale connection 문제를 방지하기 위해 `http2=False`로 강제.
- **싱글톤**: 프로세스 당 클라이언트 하나를 `_client` 전역 변수로 공유.
- **동기 클라이언트**: `supabase.Client`는 동기 클라이언트다.
  모든 Supabase 호출이 `asyncio.to_thread(...)` 안에서 실행되는 이유가 바로 이것이다.
  `supabase-py`의 `AsyncClient`를 쓰면 to_thread 래핑을 제거할 수 있다.

---

## 4. 현재 응답 시간 벤치마크

### 4-1. 벤치마크 스크립트

아래 스크립트를 `AI/scripts/benchmark_nfc.py`로 저장하고 실행한다.
실행 전제: AI 서버가 `http://localhost:8000`에서 동작 중이어야 한다.

```python
"""
리팩터링 전 응답 시간 벤치마크 스크립트.

실행 방법:
    cd AI
    python scripts/benchmark_nfc.py

환경 변수:
    BENCHMARK_BASE_URL    (기본: http://localhost:8000)
    BENCHMARK_USER_ID     (테스트용 userId)
    BENCHMARK_PRODUCT_ID  (NFC 시나리오 기준 상품 ID)
    BENCHMARK_ROUNDS      (반복 횟수, 기본: 10)
"""
import asyncio
import json
import os
import statistics
import time
import uuid

import httpx

BASE_URL = os.getenv("BENCHMARK_BASE_URL", "http://localhost:8000")
USER_ID = os.getenv("BENCHMARK_USER_ID", "test-user-id")
PRODUCT_ID = os.getenv("BENCHMARK_PRODUCT_ID", "test-product-id")
ROUNDS = int(os.getenv("BENCHMARK_ROUNDS", "10"))
POLL_INTERVAL = 1.0  # 초
TIMEOUT = 180.0      # 초


async def run_once(client: httpx.AsyncClient, round_num: int) -> dict:
    """단건 추천 요청을 발행하고 COMPLETED/FAILED 까지 대기한다."""
    job_id = str(uuid.uuid4())
    payload = {
        "jobId": job_id,
        "userId": USER_ID,
        "baseProductId": PRODUCT_ID,
        "searchPurpose": "DAILY",
        "priceTolerancePercent": 10,
        "userProfile": {
            "skinType": "DRY",
            "skinConcerns": ["dullness", "dryness"],
            "personalColor": "COOL_WINTER",
        },
    }

    t_start = time.perf_counter()
    resp = await client.post(f"{BASE_URL}/internal/agent/run", json=payload)
    resp.raise_for_status()

    # 폴링: COMPLETED 또는 FAILED 대기
    deadline = time.perf_counter() + TIMEOUT
    while time.perf_counter() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        status_resp = await client.get(f"{BASE_URL}/internal/jobs/{job_id}")
        if status_resp.status_code != 200:
            continue
        data = status_resp.json()
        status = data.get("status")
        if status in ("COMPLETED", "FAILED"):
            elapsed = time.perf_counter() - t_start
            print(
                f"  Round {round_num:02d}: {status} | {elapsed:.2f}s"
                f" | progress={data.get('progress')}%"
            )
            return {
                "round": round_num,
                "status": status,
                "elapsed": elapsed,
                "job_id": job_id,
            }

    elapsed = time.perf_counter() - t_start
    print(f"  Round {round_num:02d}: TIMEOUT after {elapsed:.2f}s")
    return {"round": round_num, "status": "TIMEOUT", "elapsed": elapsed, "job_id": job_id}


async def main():
    print(f"=== 리팩터링 전 벤치마크 (NFC 시나리오, {ROUNDS}회) ===")
    print(f"  서버: {BASE_URL}")
    print(f"  상품 ID: {PRODUCT_ID}")
    print()

    results = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for i in range(1, ROUNDS + 1):
            r = await run_once(client, i)
            results.append(r)
            await asyncio.sleep(2.0)  # 서버 부하 분산

    successful = [r["elapsed"] for r in results if r["status"] == "COMPLETED"]
    failed = [r for r in results if r["status"] != "COMPLETED"]

    print()
    print("=== 결과 요약 ===")
    print(f"  성공: {len(successful)}/{ROUNDS}")
    if successful:
        print(f"  평균: {statistics.mean(successful):.2f}s")
        print(f"  최소: {min(successful):.2f}s")
        print(f"  최대: {max(successful):.2f}s")
        if len(successful) > 1:
            print(f"  표준편차: {statistics.stdev(successful):.2f}s")
    if failed:
        print(f"  실패 job_ids: {[r['job_id'] for r in failed]}")

    output_path = "benchmark_results_before.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "rounds": ROUNDS,
                "product_id": PRODUCT_ID,
                "results": results,
                "summary": {
                    "success_count": len(successful),
                    "mean_s": round(statistics.mean(successful), 3) if successful else None,
                    "min_s": round(min(successful), 3) if successful else None,
                    "max_s": round(max(successful), 3) if successful else None,
                },
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\n  상세 결과 저장: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
```

### 4-2. Tool별 개별 소요 시간 측정 — 로깅 포인트

각 Tool의 실행 시간을 분리 측정하려면 `AI/agents/tools.py`의 각 `@tool` 함수 시작·끝에
아래 패턴의 타이밍 로그를 삽입한다 (벤치마크 목적이므로 임시 적용).

```python
import time, logging
logger = logging.getLogger("benchmark")

# 함수 시작
t0 = time.perf_counter()
result = await run_discovery(...)
logger.info(f"[BENCH] discovery elapsed={time.perf_counter()-t0:.3f}s job_id={job_id}")
```

측정 대상별 예상 위치:

| Tool | 측정 시작 | 측정 종료 |
|---|---|---|
| Discovery | `tools.py:63` (run_discovery 호출 직전) | `tools.py:75` (return 직전) |
| Score | `tools.py:110` (run_score 호출 직전) | `tools.py:124` (return 직전) |
| Alternative | `tools.py:142` (run_alternative 호출 직전) | `tools.py:152` (return 직전) |
| Collaborative | `tools.py:171` (run_collaborative 호출 직전) | `tools.py:181` (return 직전) |

### 4-3. 실측 결과

> **측정 필요** — 아래 표는 벤치마크 실행 후 기록한다.

| 지표 | 값 |
|---|---|
| 평균 응답 시간 (N=10) | — |
| 최소 응답 시간 | — |
| 최대 응답 시간 | — |
| Discovery 평균 | — |
| Score 평균 | — |
| Alternative 평균 | — |
| Collaborative 평균 | — |
| LLM round-trip 오버헤드 (추정) | 전체 시간 - (4개 tool 합산) |

---

## 5. Git 정보

| 항목 | 값 |
|---|---|
| 현재 브랜치 | `develop` |
| 마지막 커밋 해시 | `5249ae272edcf1f46f7ff04c6821dd254fc8f706` |
| 커밋 메시지 | `Update README.md` |

리팩터링 시작 전 이 커밋 해시를 `git tag pre-refactor` 등으로 태깅해두면
전후 비교가 쉬워진다.
