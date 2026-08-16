"""schema-contract.md §10 — POST /payouts, /payouts/{run_id}/retry, /webhooks/paypal.
Cloud Tasks 전용: /tasks/execute-payout, /tasks/reconcile (OIDC 필수).

`/payouts`는 승인 토큰 게이트(§7)를 통과해야 한다 — money-safety.md 절대 규칙.
승인 응답에서 PayPal을 동기 호출하지 않는다 — `/payouts`는 EXECUTING 마킹 후
Cloud Tasks에 위임하고, 실제 PayPal 호출은 `/tasks/execute-payout`에서 한다.
결과 대조는 `/tasks/reconcile`(주 경로)·`/webhooks/paypal`(보조 경로)이 `reconcile.py`의
같은 로직을 공유한다. 재발송(retry)은 아직 없다 — plan.md Phase 1 우선순위 2에서 붙인다.
"""

import os
from datetime import UTC, datetime

import requests as http_requests
from fastapi import APIRouter, Header, HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from ..guards.audit import record_audit_log
from ..guards.errors import GuardRejection
from ..guards.tokens import verify_and_burn_token
from .currency import minor_to_paypal_value
from .idempotency import build_payout_ids
from .store import (
    find_run_id_by_payout_batch_id,
    get_recipient,
    get_sender_items,
    get_settlement_run,
    get_sole_recipient_id,
    increment_recipient_monthly,
    set_sender_items,
    update_settlement_run,
)
from .paypal_client import create_payout, get_payout_batch
from .reconcile import MissingPayoutBatch, NotExecuting, RunNotFound, reconcile
from .tasks_queue import QueueNotConfigured, enqueue_execute_payout

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
def request_payout(
    body: dict,
    x_approval_token: str | None = Header(default=None, alias="X-Approval-Token"),
):
    run_id = body.get("settlement_run_id")
    run = get_settlement_run(run_id) if run_id else None
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")

    recipient_id = get_sole_recipient_id(run_id)
    if recipient_id is None:
        raise HTTPException(
            status_code=501,
            detail="multi-recipient FX aggregation not implemented — depends on matching (Track B)",
        )

    try:
        verify_and_burn_token(run_id, run, x_approval_token)
    except GuardRejection as rejection:
        record_audit_log(
            actor="api/src/guards",
            action="PAYOUT_REJECTED",
            run_id=run_id,
            reason=rejection.detail,
        )
        raise HTTPException(status_code=rejection.status_code, detail=rejection.detail)

    # schema-contract.md §7 — monthly_paid_minor 예약 가산. reconcile에서 SUCCESS가
    # 아닌 종결 상태로 확정되면 그만큼 뺀다.
    recipient = get_recipient(recipient_id)
    if recipient is not None:
        increment_recipient_monthly(recipient_id, run["total_amount_minor"])

    try:
        enqueue_execute_payout(run_id)
        note = None
    except QueueNotConfigured as e:
        note = str(e)

    record_audit_log(
        actor="api/src/payouts",
        action="PAYOUT_ENQUEUED",
        run_id=run_id,
        after={"status": "EXECUTING"},
        reason=note,
    )

    response = {"settlement_run_id": run_id, "status": "EXECUTING"}
    if note:
        response["note"] = note
    return response


@router.post("/payouts/{run_id}/retry")
def retry_payout(run_id: str):
    run = get_settlement_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    return {"settlement_run_id": run_id, "sender_items": get_sender_items(run_id)}


@router.post("/webhooks/paypal")
def paypal_webhook(body: dict):
    """schema-contract.md §8 — 보조 경로. 서명 검증 없음: 샌드박스 웹훅이 불안정해
    데모를 여기 걸지 않는다(주 경로는 /tasks/reconcile 폴링). 웹훅이 오면 같은 대조
    로직을 앞당겨 실행할 뿐, 안 와도 폴링으로 결말이 난다."""
    resource = body.get("resource", {})
    payout_batch_id = resource.get("payout_batch_id") or resource.get("batch_header", {}).get(
        "payout_batch_id"
    )
    if not payout_batch_id:
        return {"status": "ignored"}

    run_id = find_run_id_by_payout_batch_id(payout_batch_id)
    if run_id is None:
        return {"status": "ignored"}

    try:
        result = reconcile(run_id)
    except (NotExecuting, MissingPayoutBatch):
        return {"status": "ignored"}

    return {"status": "ok", "reconciled": result}


@router.post("/tasks/execute-payout")
def task_execute_payout(body: dict, authorization: str = Header(default="")):
    """Cloud Tasks가 부르는 실제 PayPal 호출 지점. schema-contract.md §3/§8, money-safety.md
    멱등성 — sender_batch_id/sender_item_id는 (run_id, recipient_id, retry_seq)에서
    결정론적으로 만든다.

    현재는 run당 recipient가 하나인 경우만 처리한다. 여러 recipient를 통화가 섞인 채로
    base_currency로 환산·합산하는 건 matching(Track B)의 결과물이 필요한데 아직 없다 —
    잘못된 금액을 조용히 보내느니 501로 막는다.
    """
    _verify_oidc(authorization)
    run_id = body.get("settlement_run_id")
    run = get_settlement_run(run_id) if run_id else None
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    if run["status"] != "EXECUTING":
        raise HTTPException(
            status_code=409,
            detail=f"settlement_run status is {run['status']}, expected EXECUTING",
        )

    recipient_id = get_sole_recipient_id(run_id)
    if recipient_id is None:
        raise HTTPException(
            status_code=501,
            detail="multi-recipient FX aggregation not implemented — depends on matching (Track B)",
        )
    recipient = get_recipient(recipient_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail=f"unknown recipient_id: {recipient_id}")

    retry_seq = run.get("retry_seq", 0)
    sender_batch_id, sender_item_id = build_payout_ids(run_id, recipient_id, retry_seq)
    amount_minor = run["total_amount_minor"]
    currency = run["base_currency"]
    paypal_value = minor_to_paypal_value(amount_minor, currency)

    try:
        batch = create_payout(
            sender_batch_id,
            [
                {
                    "recipient_type": "EMAIL",
                    "amount": {"value": paypal_value, "currency": currency},
                    "receiver": recipient["paypal_email"],
                    "sender_item_id": sender_item_id,
                }
            ],
        )
    except http_requests.HTTPError as e:
        record_audit_log(
            actor="api/src/payouts", action="PAYOUT_CALL_FAILED", run_id=run_id, reason=str(e)
        )
        raise HTTPException(status_code=502, detail="PayPal payout call failed") from e

    payout_batch_id = batch.get("batch_header", {}).get("payout_batch_id")
    detail = get_payout_batch(payout_batch_id) if payout_batch_id else {}
    item = next(iter(detail.get("items", [])), {})
    transaction_status = item.get("transaction_status", "PENDING")
    internal_status = (
        transaction_status
        if transaction_status in {"SUCCESS", "FAILED", "UNCLAIMED", "PENDING"}
        else "OTHER"
    )

    now = datetime.now(UTC)
    sender_item = {
        "sender_item_id": sender_item_id,
        "settlement_run_id": run_id,
        "recipient_id": recipient_id,
        "receiver_email": recipient["paypal_email"],
        "amount_minor": amount_minor,
        "currency": currency,
        "paypal_value": paypal_value,
        "payout_item_id": item.get("payout_item_id"),
        "paypal_transaction_status": transaction_status,
        "status": internal_status,
        "retry_of": None,
        "created_at": now,
        "updated_at": now,
    }
    set_sender_items(run_id, [sender_item])
    update_settlement_run(run_id, {"payout_batch_id": payout_batch_id, "updated_at": now})

    record_audit_log(
        actor="api/src/payouts",
        action="PAYOUT_CALLED",
        run_id=run_id,
        after={"payout_batch_id": payout_batch_id, "sender_batch_id": sender_batch_id},
    )

    return {
        "settlement_run_id": run_id,
        "payout_batch_id": payout_batch_id,
        "sender_items": [sender_item],
    }


@router.post("/tasks/reconcile")
def task_reconcile(body: dict, authorization: str = Header(default="")):
    """schema-contract.md §8 — 주 경로. /payouts enqueue 시 RECONCILE_DELAY_SECONDS 뒤에
    Cloud Tasks가 부른다. 미종결이면 이 라우트가 스스로를 재예약해야 하는데, 그 큐잉은
    tasks_queue.py와 마찬가지로 아직 없다 — pending 응답을 보고 데모/테스트에서 다시
    호출해 재폴링을 시뮬레이션한다."""
    _verify_oidc(authorization)
    run_id = body.get("settlement_run_id")
    if not run_id:
        raise HTTPException(status_code=400, detail="settlement_run_id required")

    try:
        return reconcile(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    except NotExecuting as e:
        raise HTTPException(
            status_code=409, detail=f"settlement_run status is {e.status}, expected EXECUTING"
        )
    except MissingPayoutBatch:
        raise HTTPException(
            status_code=409,
            detail="run is EXECUTING but has no payout_batch_id — execute-payout must run first",
        )
