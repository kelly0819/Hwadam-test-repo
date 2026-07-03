from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from graph.pipeline import pipeline
from services import job_updater

router = APIRouter()


class UserProfileInput(BaseModel):
    skinType: str | None = None
    skinConcerns: list[str] = []
    personalColor: str | None = None


class AgentRunRequest(BaseModel):
    jobId: str
    userId: str
    baseProductId: str | None = None
    searchPurpose: str | None = None   # DAILY | OFFICE | DATE
    priceTolerancePercent: int = 10
    userProfile: UserProfileInput = UserProfileInput()



async def _save_user_context(req: AgentRunRequest) -> None:
    """추천 완료 후 검색 맥락을 user_context_rags에 저장. 실패해도 무시."""
    try:
        from services.embedding_service import EmbeddingService
        from db.supabase_client import get_supabase
        from db.product_reader import get_product_meta

        sb = get_supabase()

        user_check = await asyncio.to_thread(
            lambda: sb.table("users").select("id").eq("id", req.userId).limit(1).execute()
        )
        if not user_check.data:
            print(f"[user_context_rags] userId {req.userId} not found in users, skipping")
            return

        profile = req.userProfile
        concerns = "·".join(profile.skinConcerns) if profile.skinConcerns else ""
        context_text = (
            f"{profile.skinType or ''} 피부, {profile.personalColor or ''} 톤"
            + (f", {concerns} 고민" if concerns else "")
            + f". {req.searchPurpose or ''} 용도로 추천 요청."
            + f" 가격 허용 폭 ±{req.priceTolerancePercent}%."
        ).strip()

        emb = EmbeddingService.get()
        vec = await asyncio.to_thread(emb.embed, context_text)

        meta = await asyncio.to_thread(get_product_meta, req.baseProductId or "")
        category = meta["category"] if meta else None

        await asyncio.to_thread(
            lambda: sb.table("user_context_rags").insert({
                "user_id": req.userId,
                "context_text": context_text,
                "embedding": vec,
                "category": category,
            }).execute()
        )
    except Exception as e:
        print(f"[user_context_rags] 저장 실패: {e}")


async def _run_agent(req: AgentRunRequest) -> None:
    try:
        await job_updater.update(req.jobId, status="IN_PROGRESS", progress=0)

        initial_state = {
            "job_id": req.jobId,
            "user_id": req.userId,
            "base_product_id": req.baseProductId or "",
            "search_purpose": req.searchPurpose,
            "price_tolerance_percent": req.priceTolerancePercent,
            "user_profile": {
                "skinType": req.userProfile.skinType,
                "skinConcerns": req.userProfile.skinConcerns,
                "personalColor": req.userProfile.personalColor,
            },
            "candidates": [],
            "intent_vector": [],
            "score_result": [],
            "enabled_agents": [],
            "alternative_result": [],
            "collaborative_result": [],
            "final_result": None,
        }

        await pipeline.ainvoke(initial_state)
        # DB 저장(COMPLETED)은 merge_node 내부에서 처리됨
        await _save_user_context(req)

    except Exception as e:
        await job_updater.update(req.jobId, status="FAILED", error_msg=str(e))


@router.post("/agent/run", status_code=202)
async def agent_run(req: AgentRunRequest, background_tasks: BackgroundTasks) -> dict:
    background_tasks.add_task(_run_agent, req)
    return {"jobId": req.jobId, "status": "accepted"}
