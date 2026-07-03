"""
StateGraph 기반 추천 파이프라인.

기존 create_react_agent(action_model.py) 대비 변경 사항:
- LLM이 "다음 tool 선택"을 판단하지 않음 — 실행 순서가 코드로 고정됨
- _intent_store 전역 dict 제거 — state["intent_vector"]로 직접 전달
- candidates / exclude_ids를 str 직렬화 없이 list[dict] 그대로 전달
- alternative / collaborative가 score 완료 후 병렬 실행됨
- LLM 호출은 aiReason 생성 1회만 남음 (Merge 노드)

그래프 구조:
    START
      └─ discovery_node
            └─ score_node
                  ├─ alternative_node ─┐
                  └─ collaborative_node─┤
                                        └─ merge_node ─ END
"""
from __future__ import annotations

import asyncio
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from datetime import datetime, timezone

from agents.alternative_agent import run_alternative
from agents.collaborative_agent import run_collaborative
from agents.discovery_agent import run_discovery
from agents.score_agent import run_score
from db.score_reader import get_insights
from db.supabase_client import get_supabase
from models.agent_context import UserProfile
from prompts.weight_adjustment import (
    WEIGHT_ADJUSTMENT_SYSTEM,
    build_weight_adjustment_user_prompt,
)
from services import job_updater
from services.qwen_client import get_qwen_llm
from services.weight_validator import validate_and_normalize


# ── State ─────────────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    # 입력값 (agent_router가 채움)
    job_id: str
    user_id: str
    base_product_id: str
    search_purpose: Optional[str]
    price_tolerance_percent: int
    user_profile: dict          # camelCase 원본 {"skinType", "personalColor", "skinConcerns"}

    # discovery 출력
    candidates: list[dict]      # [{"product_id": str, "similarity": float}, ...]
    intent_vector: list[float]  # bge-m3 1024차원

    # score 출력
    score_result: list[dict]    # [{"productId", "totalScore", "breakdown"}, ...]

    # score 결정: 실행할 보조 에이전트 목록 (["alternative", "collaborative"] 중 부분집합)
    enabled_agents: list[str]

    # alternative / collaborative 출력 (enabled_agents에 따라 선택적 실행)
    alternative_result: list[dict]    # [{"id", "name", "brand", "imageUrl", "price", "ingredientSimilarity"}, ...]
    collaborative_result: list[dict]  # [{"id", "name", "brand", "imageUrl", "price", "satisfactionPercent"}, ...]

    # merge 출력 (최종 결과)
    final_result: Optional[dict]


# ── Step 1: Discovery 노드 ─────────────────────────────────────────────────────

async def discovery_node(state: AgentState) -> dict:
    """
    run_discovery()를 감싸는 노드.

    - state["user_profile"] (camelCase dict)를 UserProfile로 변환
    - 반환값을 state["candidates"], state["intent_vector"]에 직접 저장
    - str 직렬화 없음, LLM 판단 없음
    """
    profile_dict = state["user_profile"]
    profile = UserProfile(
        user_id=state["user_id"],
        skin_type=profile_dict.get("skinType"),
        personal_color=profile_dict.get("personalColor"),
        skin_concerns=profile_dict.get("skinConcerns", []),
    )

    result = await run_discovery(
        user_profile=profile,
        base_product_id=state["base_product_id"],
        search_purpose=state.get("search_purpose"),
        price_tolerance_percent=state.get("price_tolerance_percent", 10),
    )

    if state.get("job_id"):
        await job_updater.update(
            state["job_id"], step="후보 탐색", progress=25, status="IN_PROGRESS"
        )

    return {
        "candidates": result["candidates"],      # list[ProductMatch]
        "intent_vector": result["intent_vector"], # list[float]
    }


# ── 에이전트 레지스트리 ────────────────────────────────────────────────────────
# 새 보조 에이전트 추가 시 이 dict에 이름: 노드함수 형태로 등록하면
# 라우터·그래프 엣지에 자동 반영됨.

AGENT_REGISTRY: dict[str, object] = {}  # 노드 함수는 아래 정의 후 채워짐


# ── Step 2: Score 노드 ────────────────────────────────────────────────────────

_DEFAULT_AGENTS: list[str] = ["alternative", "collaborative"]


async def score_node(state: AgentState) -> dict:
    """
    가중치 + enabled_agents를 LLM 1회 호출로 결정한 뒤 run_score() 실행.

    변경 전: run_score() 내부에서 가중치 LLM 호출 → 결과만 반환
    변경 후: score_node가 통합 LLM 호출 → weights + enabled_agents 동시 결정
             → weights를 run_score()에 주입 (내부 LLM 호출 스킵)
             → enabled_agents를 state에 저장 → 라우터가 fan-out 경로 결정
    """
    # 1. state에서 직접 읽기
    candidates: list[dict] = state["candidates"]
    intent_vector: list[float] = state["intent_vector"]
    candidate_ids = [c["product_id"] for c in candidates if "product_id" in c]

    # 2. camelCase → snake_case 명시적 변환
    profile_dict = state["user_profile"]
    profile_snake = {
        "skin_type": profile_dict.get("skinType"),
        "personal_color": profile_dict.get("personalColor"),
        "skin_concerns": profile_dict.get("skinConcerns", []),
    }

    # 3. base_product_price: DB에서 직접 조회
    base_product_id = state["base_product_id"]
    base_insights = await asyncio.to_thread(get_insights, [base_product_id])
    base_insight = base_insights.get(base_product_id)
    target_price: Optional[int] = base_insight["lowest_price"] if base_insight else None

    # 4. 통합 LLM 호출 1회: 가중치 + enabled_agents 동시 결정
    llm = get_qwen_llm()
    raw = await llm.chat_json(
        system=WEIGHT_ADJUSTMENT_SYSTEM,
        user=build_weight_adjustment_user_prompt(
            search_purpose=state.get("search_purpose"),
            price_tolerance_percent=state.get("price_tolerance_percent", 10),
            skin_concerns=profile_dict.get("skinConcerns"),
            personal_color=profile_dict.get("personalColor"),
        ),
    )

    # 5. enabled_agents 추출 (LLM 실패 시 기본값: 둘 다 실행)
    enabled_agents: list[str] = _DEFAULT_AGENTS
    if isinstance(raw, dict):
        raw_agents = raw.get("enabled_agents")
        if isinstance(raw_agents, list):
            valid = [a for a in raw_agents if a in AGENT_REGISTRY]
            enabled_agents = valid if valid else _DEFAULT_AGENTS

    # 6. weights 추출 (enabled_agents 키 제거 후 validate)
    weights_raw = {k: v for k, v in (raw or {}).items() if k != "enabled_agents"}
    weights = validate_and_normalize(weights_raw)

    # 7. run_score() — 검증된 weights 주입, 내부 LLM 호출 스킵
    result = await run_score(
        intent_vector=intent_vector,
        candidate_ids=candidate_ids,
        user_profile=profile_snake,
        search_purpose=state.get("search_purpose"),
        price_tolerance_percent=state.get("price_tolerance_percent", 10),
        target_price=target_price,
        weights=weights,
    )

    if state.get("job_id"):
        await job_updater.update(
            state["job_id"], step="AI 분석", progress=60, status="IN_PROGRESS"
        )

    return {"score_result": result, "enabled_agents": enabled_agents}


# ── 라우터: score → (alternative | collaborative) 선택적 fan-out ───────────────

def route_after_score(state: AgentState) -> list[str]:
    """
    state["enabled_agents"]를 기준으로 실행할 노드 목록 반환.
    빈 리스트나 레지스트리에 없는 이름은 제외.
    결과가 없으면 전체 레지스트리 키로 폴백.
    """
    enabled = state.get("enabled_agents") or _DEFAULT_AGENTS
    valid = [name for name in enabled if name in AGENT_REGISTRY]
    return valid if valid else list(AGENT_REGISTRY.keys())


# ── Step 3: Alternative / Collaborative 노드 (병렬 fan-out) ──────────────────

def _build_exclude_ids(state: AgentState) -> list[str]:
    """candidates product_id 전체 + base_product_id를 exclude 리스트로 조립."""
    ids = [c["product_id"] for c in state["candidates"] if "product_id" in c]
    base = state["base_product_id"]
    if base and base not in ids:
        ids = [base] + ids
    return ids


async def alternative_node(state: AgentState) -> dict:
    """
    run_alternative()를 감싸는 노드.

    - exclude_ids: state["candidates"]에서 코드로 직접 추출 (JSON 문자열 조립 없음)
    - base_product_id도 exclude에 포함 (기준 상품 중복 노출 방지)
    - 결과를 state["alternative_result"]에 저장
    """
    exclude_ids = _build_exclude_ids(state)

    result = await run_alternative(
        base_product_id=state["base_product_id"],
        exclude_ids=exclude_ids,
        top_k=5,
    )

    return {"alternative_result": [r.model_dump(by_alias=True) for r in result]}


async def collaborative_node(state: AgentState) -> dict:
    """
    run_collaborative()를 감싸는 노드.

    - exclude_ids: state["candidates"]에서 코드로 직접 추출
    - skin_type / personal_color: score_node와 동일하게 camelCase → snake_case 코드 변환
    - 결과를 state["collaborative_result"]에 저장
    """
    exclude_ids = _build_exclude_ids(state)

    profile_dict = state["user_profile"]
    skin_type: Optional[str] = profile_dict.get("skinType")
    personal_color: Optional[str] = profile_dict.get("personalColor")

    result = await run_collaborative(
        user_id=state["user_id"],
        skin_type=skin_type,
        personal_color=personal_color,
        exclude_ids=exclude_ids,
        top_k=5,
    )

    return {"collaborative_result": [r.model_dump(by_alias=True) for r in result]}


# ── Step 4: Merge 노드 ────────────────────────────────────────────────────────

_MATCH_LABELS = [
    (85, "인생템 확률 매칭"),
    (70, "높은 적합도"),
    (50, "괜찮은 선택"),
    (0,  "추천 상품"),
]

_AIREASON_SYSTEM = (
    "당신은 화장품 추천 전문가입니다. "
    "아래 사용자 정보와 점수 분석을 바탕으로 추천 이유를 한국어 1~2문장으로 간결하게 작성하세요. "
    "JSON, 마크다운, 추가 설명 없이 순수 텍스트만 반환하세요."
)


async def _generate_ai_reason(
    user_profile: dict,
    search_purpose: Optional[str],
    top_score: dict,
) -> str:
    """aiReason 전용 LLM 호출 1회."""
    bd = top_score.get("breakdown", {})
    concerns = ", ".join(user_profile.get("skinConcerns", []))
    user_prompt = (
        f"피부 타입: {user_profile.get('skinType', '')}\n"
        f"퍼스널 컬러: {user_profile.get('personalColor', '')}\n"
        f"피부 고민: {concerns or '없음'}\n"
        f"구매 목적: {search_purpose or '일반'}\n"
        f"점수 분석 — 예산 적합도: {bd.get('budgetFit', 0)}, "
        f"가성비: {bd.get('priceValue', 0)}, "
        f"리뷰 적합도: {bd.get('reviewScore', 0)}, "
        f"개인화: {bd.get('personalization', 0)}"
    )
    llm = get_qwen_llm()
    raw = await llm.chat(system=_AIREASON_SYSTEM, user=user_prompt, response_json=False)
    return raw.strip() or "사용자 피부 조건과 예산에 맞는 상품을 추천합니다."


def _write_result_to_db(job_id: str, result: dict) -> None:
    """recommendation_jobs 테이블에 최종 결과 저장 (sync, to_thread용)."""
    get_supabase().table("recommendation_jobs").update({
        "result": result,
        "status": "COMPLETED",
        "step": "루틴 생성",
        "progress": 100,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()


async def merge_node(state: AgentState) -> dict:
    """
    alternative / collaborative 결과를 합쳐 최종 응답 JSON 조립.

    - matchScore: score_result 최상위 totalScore
    - matchLabel: 점수 구간으로 코드 결정 (LLM 불필요)
    - aiReason: LLM 1회 호출 (자연어 생성이 필요한 유일한 필드)
    - similarUserProducts / alternativeProducts: 각 노드 결과 직접 사용
    - 완료 후 recommendation_jobs 테이블에 저장
    """
    # 1. matchScore: score_result 최상위 점수
    score_result: list[dict] = state.get("score_result") or []
    top_score = max(score_result, key=lambda x: x.get("totalScore", 0)) if score_result else {}
    match_score = min(100, int(top_score.get("totalScore", 0)))

    # 2. matchLabel: 점수 구간으로 코드 결정
    match_label = next(label for threshold, label in _MATCH_LABELS if match_score >= threshold)

    # 3. aiReason: 최소 LLM 1회 호출
    ai_reason = await _generate_ai_reason(
        user_profile=state["user_profile"],
        search_purpose=state.get("search_purpose"),
        top_score=top_score,
    )

    # 4. 최종 결과 조립 (기존 응답 구조와 동일)
    # .get() + or [] : enabled_agents에서 스킵된 에이전트 결과가 없어도 안전
    final_result = {
        "matchScore": match_score,
        "matchLabel": match_label,
        "aiReason": ai_reason,
        "similarUserProducts": state.get("collaborative_result") or [],
        "alternativeProducts": state.get("alternative_result") or [],
    }

    # 5. Supabase 저장
    if state.get("job_id"):
        await asyncio.to_thread(_write_result_to_db, state["job_id"], final_result)

    return {"final_result": final_result}


# ── 레지스트리 채우기 ─────────────────────────────────────────────────────────
# 새 에이전트 추가: 노드 함수 정의 후 이 dict에 등록하면 라우터·그래프에 자동 반영.

AGENT_REGISTRY["alternative"]   = alternative_node
AGENT_REGISTRY["collaborative"] = collaborative_node


# ── 그래프 조립 ────────────────────────────────────────────────────────────────

def build_pipeline() -> StateGraph:
    g = StateGraph(AgentState)

    g.add_node("discovery", discovery_node)
    g.add_node("score", score_node)

    # 레지스트리 기반으로 노드 동적 등록
    for name, fn in AGENT_REGISTRY.items():
        g.add_node(name, fn)
        g.add_edge(name, "merge")          # 각 에이전트 → merge (fan-in)

    g.add_node("merge", merge_node)

    # 고정 순서
    g.set_entry_point("discovery")
    g.add_edge("discovery", "score")

    # score → enabled_agents 기반 선택적 fan-out
    g.add_conditional_edges(
        "score",
        route_after_score,
        {name: name for name in AGENT_REGISTRY},   # path_map: 이름 → 노드명 동일
    )

    g.add_edge("merge", END)

    return g


pipeline = build_pipeline().compile()
