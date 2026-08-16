"""schema-contract.md §10 — POST /settlements/runs/{run_id}/approve.

money-safety.md 승인 게이트: DRAFT → APPROVED CAS, 한도 캡 검사, 토큰 발급.
Firestore가 아직 안 붙어 있어 `payouts.fixtures`가 들고 있는 인메모리 dict를
DB처럼 mutate한다 — 실제 영속화·트랜잭션은 Firestore 연동 시 대체한다.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from ..payouts.fixtures import get_settlement_run
from .audit import record_audit_log
from .limits import check_caps
from .tokens import issue_token

router = APIRouter()


@router.post("/settlements/runs/{run_id}/approve")
def approve_settlement_run(run_id: str, body: dict | None = None):
    run = get_settlement_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")

    if run["status"] != "DRAFT":
        raise HTTPException(
            status_code=409,
            detail=f"settlement_run status is {run['status']}, expected DRAFT",
        )

    violation = check_caps(run)
    if violation:
        record_audit_log(
            actor="api/src/guards", action="PAYOUT_REJECTED", run_id=run_id, reason=violation
        )
        raise HTTPException(status_code=403, detail=violation)

    approved_by = (body or {}).get("approved_by", "demo_approver")
    token = issue_token(run)

    now = datetime.now(UTC).isoformat()
    run["status"] = "APPROVED"
    run["approved_by"] = approved_by
    run["approved_at"] = now
    run["fx_locked_at"] = now

    record_audit_log(
        actor=approved_by,
        actor_type="HUMAN",
        action="RUN_APPROVED",
        run_id=run_id,
        after={"status": "APPROVED"},
    )

    response = {k: v for k, v in run.items() if k != "approval_token_hash"}
    response["approval_token"] = token
    return response
