"""schema-contract.md §10 — POST /payouts, /payouts/{run_id}/retry, /webhooks/paypal.
Cloud Tasks 전용: /tasks/execute-payout, /tasks/reconcile (OIDC 필수).

`/payouts`는 승인 토큰 게이트(§7)를 통과해야 한다 — money-safety.md 절대 규칙.
PayPal 실제 호출, 멱등성, 재발송 로직은 아직 없다 — plan.md Phase 1 우선순위 2에서
붙인다.
"""

import os

from fastapi import APIRouter, Header, HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from ..guards.audit import record_audit_log
from ..guards.errors import GuardRejection
from ..guards.tokens import verify_and_burn_token
from .fixtures import get_sender_items, get_settlement_run

router = APIRouter()
_google_request = google_requests.Request()


def _verify_oidc(authorization: str) -> None:
    """schema-contract.md §9 — Cloud Tasks 전용 라우트 진입점. main.py의 /tasks/ping과
    동일한 검증 방식이다."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    token = authorization.removeprefix("Bearer ")
    audience = os.environ["OIDC_AUDIENCE"]
    try:
        id_token.verify_oauth2_token(token, _google_request, audience=audience)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post("/payouts")
def create_payout(
    body: dict,
    x_approval_token: str | None = Header(default=None, alias="X-Approval-Token"),
):
    run_id = body.get("settlement_run_id")
    run = get_settlement_run(run_id) if run_id else None
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")

    try:
        verify_and_burn_token(run, x_approval_token)
    except GuardRejection as rejection:
        record_audit_log(
            actor="api/src/guards",
            action="PAYOUT_REJECTED",
            run_id=run_id,
            reason=rejection.detail,
        )
        raise HTTPException(status_code=rejection.status_code, detail=rejection.detail)

    record_audit_log(
        actor="api/src/payouts",
        action="PAYOUT_ENQUEUED",
        run_id=run_id,
        after={"status": run["status"]},
    )

    return {
        "settlement_run_id": run_id,
        "status": run["status"],
        "sender_items": get_sender_items(run_id),
    }


@router.post("/payouts/{run_id}/retry")
def retry_payout(run_id: str):
    run = get_settlement_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    return {"settlement_run_id": run_id, "sender_items": get_sender_items(run_id)}


@router.post("/webhooks/paypal")
def paypal_webhook(body: dict):
    return {"status": "ok"}


@router.post("/tasks/execute-payout")
def task_execute_payout(body: dict, authorization: str = Header(default="")):
    _verify_oidc(authorization)
    run_id = body.get("settlement_run_id")
    return {"settlement_run_id": run_id, "sender_items": get_sender_items(run_id)}


@router.post("/tasks/reconcile")
def task_reconcile(body: dict, authorization: str = Header(default="")):
    _verify_oidc(authorization)
    run_id = body.get("settlement_run_id")
    return {"settlement_run": get_settlement_run(run_id), "sender_items": get_sender_items(run_id)}
