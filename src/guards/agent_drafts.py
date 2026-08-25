"""schema-contract.md §9 — agent가 draft를 쓰는 유일한 창구.

`agent`는 Firestore SDK를 직접 쓰지 않는다(architecture.md) — 이 라우트가 대리 쓰기를
한다. Cloud Tasks가 아니라 `agent` 서비스 계정이 직접 부르므로 OIDC 검증은 동일하되
호출자가 다르다(infra/iam.tf `agent_invoker_api` 참조).

`/agents/audit`는 before_tool_callback이 툴 호출 시도(거부 포함)를 남기는 경로다 —
money-safety.md "모든 툴 호출을 audit_logs에 남긴다"는 성공한 draft 쓰기뿐 아니라
거부된 시도도 포함한다.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException

from ..payouts.store import get_client
from .audit import record_audit_log
from .oidc import verify_oidc
from .tasks import enqueue_task

router = APIRouter()

_VALID_AGENTS = {"CLAIMANT", "EXECUTOR"}
_VALID_TARGET_TYPES = {"RECEIPT", "SETTLEMENT_RUN"}


@router.post("/agents/drafts")
def write_agent_draft(body: dict, authorization: str = Header(default="")):
    verify_oidc(authorization)

    agent = body.get("agent")
    target_type = body.get("target_type")
    target_id = body.get("target_id")
    task_id = body.get("task_id")
    payload = body.get("payload")
    if agent not in _VALID_AGENTS:
        raise HTTPException(status_code=400, detail=f"invalid agent: {agent}")
    if target_type not in _VALID_TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"invalid target_type: {target_type}")
    if not target_id or not task_id or payload is None:
        raise HTTPException(status_code=400, detail="target_id, task_id, payload required")

    # schema-contract.md §2 — task_id가 멱등키다. 문서 ID로 재사용해 Cloud Tasks
    # 재시도가 새 draft를 쌓지 않고 같은 draft를 덮어쓰게 한다.
    draft = {
        "draft_id": f"drf_{task_id}",
        "agent": agent,
        "target_type": target_type,
        "target_id": target_id,
        "task_id": task_id,
        "payload": payload,
        "created_at": datetime.now(UTC),
    }
    get_client().collection("agent_drafts").document(task_id).set(draft)

    record_audit_log(
        actor=f"agent/{agent.lower()}",
        actor_type="AGENT",
        action="AGENT_DRAFT_WRITTEN",
        run_id=target_id if target_type == "SETTLEMENT_RUN" else None,
        after={"draft_id": draft["draft_id"]},
    )

    # 청구자 draft는 api가 읽어 상태 전이로 반영한다(§9). 실패해도 draft 쓰기를
    # 되돌리지 않는다 — 에이전트에게 500을 주면 Cloud Tasks가 LLM 호출을 다시 태운다.
    # 감사 로그에 task_id를 남기는 게 중요하다: 이 라우트는 task_id로만 draft를
    # 찾으므로, 실패 건을 audit_logs에서 뽑아 그대로 재큐잉할 수 있어야 한다.
    if agent == "CLAIMANT":
        try:
            enqueue_task("/tasks/apply-claimant-draft", {"task_id": task_id})
        except Exception as e:
            record_audit_log(
                actor="api/src/guards",
                action="CLAIMANT_DRAFT_APPLY_ENQUEUE_FAILED",
                reason=str(e),
                after={"draft_id": draft["draft_id"], "task_id": task_id},
            )

    return {"draft_id": draft["draft_id"], "status": "ok"}


@router.post("/agents/audit")
def write_agent_tool_audit(body: dict, authorization: str = Header(default="")):
    """agent-tools.md before_tool_callback — 툴 호출 시도를 남긴다. 거부됐어도 남긴다."""
    verify_oidc(authorization)

    agent = body.get("agent")
    action = body.get("action")
    if agent not in _VALID_AGENTS or not action:
        raise HTTPException(status_code=400, detail="agent, action required")

    record_audit_log(
        actor=f"agent/{agent.lower()}",
        actor_type="AGENT",
        action=action,
        run_id=body.get("run_id"),
        reason=body.get("reason"),
    )
    return {"status": "ok"}
