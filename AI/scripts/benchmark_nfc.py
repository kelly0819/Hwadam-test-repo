"""
리팩터링 전후 응답 시간 벤치마크 (NFC 시나리오).

각 라운드마다:
  1. Supabase recommendation_jobs에 PENDING row INSERT
  2. POST /internal/agent/run 호출
  3. COMPLETED / FAILED 될 때까지 폴링
  4. 경과 시간 측정

환경 변수 (없으면 아래 기본값 사용):
  BENCHMARK_BASE_URL    기본: http://localhost:8000
  BENCHMARK_USER_ID     기본: aa000000-0000-0000-0000-000000000001
  BENCHMARK_PRODUCT_ID  기본: 19e7105a-b848-4e24-8088-c1ce3761d864
  BENCHMARK_ROUNDS      기본: 10
  SUPABASE_URL
  SUPABASE_SERVICE_KEY
"""
import asyncio
import json
import os
import statistics
import time
import uuid
from datetime import datetime, timezone

import httpx
from supabase import create_client

# ── 설정 ──────────────────────────────────────────────────────────────────────
BASE_URL    = os.getenv("BENCHMARK_BASE_URL",   "http://localhost:8000")
USER_ID     = os.getenv("BENCHMARK_USER_ID",    "aa000000-0000-0000-0000-000000000001")
PRODUCT_ID  = os.getenv("BENCHMARK_PRODUCT_ID", "19e7105a-b848-4e24-8088-c1ce3761d864")
ROUNDS      = int(os.getenv("BENCHMARK_ROUNDS", "10"))
POLL_INTERVAL = 3.0   # 초
TIMEOUT       = 180.0 # 초

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

PAYLOAD_TEMPLATE = {
    "userId": USER_ID,
    "baseProductId": PRODUCT_ID,
    "searchPurpose": "DAILY",
    "priceTolerancePercent": 10,
    "userProfile": {
        "skinType": "DRY",
        "skinConcerns": ["dryness"],
        "personalColor": "SPRING",
    },
}


def insert_job_row(sb, job_id: str) -> None:
    """각 라운드 전 Supabase에 PENDING row를 INSERT."""
    now = datetime.now(timezone.utc).isoformat()
    sb.table("recommendation_jobs").insert({
        "id": job_id,
        "user_id": USER_ID,
        "base_product_id": PRODUCT_ID,
        "status": "PENDING",
        "progress": 0,
        "search_purpose": "DAILY",
        "price_tolerance_percent": 10,
        "step": None,
        "result": None,
        "error_msg": None,
        "created_at": now,
        "updated_at": now,
    }).execute()


def poll_job_status(sb, job_id: str) -> dict:
    """COMPLETED / FAILED 될 때까지 동기 폴링. 결과 row 반환."""
    r = sb.table("recommendation_jobs") \
        .select("status,progress,step,error_msg") \
        .eq("id", job_id).execute()
    return r.data[0] if r.data else {}


async def run_once(client: httpx.AsyncClient, sb, round_num: int) -> dict:
    job_id = str(uuid.uuid4())

    # 1. Supabase INSERT (FastAPI가 UPDATE만 하므로 반드시 먼저 생성)
    insert_job_row(sb, job_id)

    payload = {**PAYLOAD_TEMPLATE, "jobId": job_id}

    # 2. 에이전트 실행 요청
    t_start = time.perf_counter()
    resp = await client.post(f"{BASE_URL}/internal/agent/run", json=payload)
    resp.raise_for_status()

    # 3. COMPLETED / FAILED 폴링
    deadline = time.perf_counter() + TIMEOUT
    final_status = "TIMEOUT"
    while time.perf_counter() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        row = poll_job_status(sb, job_id)
        status = row.get("status", "")
        if status in ("COMPLETED", "FAILED"):
            final_status = status
            break

    elapsed = time.perf_counter() - t_start
    step = row.get("step") if final_status != "TIMEOUT" else "-"
    err  = row.get("error_msg") if final_status != "TIMEOUT" else "timeout"

    tag = "✓" if final_status == "COMPLETED" else "✗"
    print(f"  [{round_num:02d}] {tag} {final_status:<10} {elapsed:6.2f}s  step={step}  err={err}")

    return {
        "round": round_num,
        "job_id": job_id,
        "status": final_status,
        "elapsed_s": round(elapsed, 3),
    }


async def main():
    print(f"=== 벤치마크 시작 (N={ROUNDS}) ===")
    print(f"  서버:   {BASE_URL}")
    print(f"  상품:   {PRODUCT_ID}")
    print(f"  유저:   {USER_ID}")
    print()

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    results = []

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for i in range(1, ROUNDS + 1):
            r = await run_once(client, sb, i)
            results.append(r)
            if i < ROUNDS:
                await asyncio.sleep(2.0)  # 라운드 간 서버 부하 분산

    # ── 요약 ──────────────────────────────────────────────────────────────────
    ok  = [r["elapsed_s"] for r in results if r["status"] == "COMPLETED"]
    fail = [r for r in results if r["status"] != "COMPLETED"]

    print()
    print("=== 결과 요약 ===")
    print(f"  성공: {len(ok)}/{ROUNDS}")
    if ok:
        print(f"  평균: {statistics.mean(ok):.2f}s")
        print(f"  최소: {min(ok):.2f}s")
        print(f"  최대: {max(ok):.2f}s")
        if len(ok) > 1:
            print(f"  표준편차: {statistics.stdev(ok):.2f}s")
    if fail:
        print(f"  실패/타임아웃: {[r['job_id'] for r in fail]}")

    summary = {
        "rounds": ROUNDS,
        "product_id": PRODUCT_ID,
        "results": results,
        "summary": {
            "success_count": len(ok),
            "mean_s":   round(statistics.mean(ok),   3) if ok else None,
            "min_s":    round(min(ok),               3) if ok else None,
            "max_s":    round(max(ok),               3) if ok else None,
            "stdev_s":  round(statistics.stdev(ok),  3) if len(ok) > 1 else None,
        },
    }
    out = "benchmark_results_after.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n  상세 결과 저장: {out}")


if __name__ == "__main__":
    asyncio.run(main())
