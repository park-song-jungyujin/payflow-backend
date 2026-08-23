"""schema-contract.md §10 — POST /payouts, /payouts/{run_id}/retry, /webhooks/paypal.
Cloud Tasks 전용: /tasks/execute-payout, /tasks/reconcile (OIDC 필수).

`/payouts`는 승인 토큰 게이트(§7)를 통과해야 한다 — money-safety.md 절대 규칙.
승인 응답에서 PayPal을 동기 호출하지 않는다 — `/payouts`는 EXECUTING 마킹 후
Cloud Tasks에 위임하고, 실제 PayPal 호출은 `/tasks/execute-payout`에서 한다.
결과 대조는 `/tasks/reconcile`(주 경로)·`/webhooks/paypal`(보조 경로)이 `reconcile.py`의
같은 로직을 공유한다. `/payouts` enqueue 시 RECONCILE_DELAY_SECONDS 뒤에 reconcile도 함께
예약하고, 미종결이면 task_reconcile이 스스로 재예약한다.

재발송(`/payouts/{run_id}/retry`)은 schema-contract.md §8 "재발송" 규칙대로 새 승인
토큰이 있어야 한다 — FAILED run을 다시 approve(guards/routes.py)해서 토큰을 받은 뒤
호출한다. retry_seq를 올려 build_payout_ids로 새 배치 ID를 만든다.
"""

import os
from datetime import UTC, datetime

import requests as http_requests
from fastapi import APIRouter, Header, HTTPException

from ..guards.audit import record_audit_log
from ..guards.errors import GuardRejection
from ..guards.oidc import verify_oidc
from ..guards.tokens import verify_and_burn_token
from .amounts import per_recipient_amounts
from .currency import UnsupportedPayoutCurrency, assert_supported_payout_currency, minor_to_paypal_value
from .idempotency import build_payout_ids
from .store import (
    find_run_id_by_payout_batch_id,
    get_recipient,
    get_settlement_run,
    increment_recipient_monthly,
    set_sender_items,
    update_settlement_run,
)
from .paypal_client import create_payout, get_payout_batch
from .reconcile import MissingPayoutBatch, NotExecuting, RunNotFound, reconcile
from .tasks_queue import QueueNotConfigured, enqueue_execute_payout, enqueue_reconcile

router = APIRouter()


@router.post("/payouts")
def request_payout(
    body: dict,
    x_approval_token: str | None = Header(default=None, alias="X-Approval-Token"),
):
    run_id = body.get("settlement_run_id")
    run = get_settlement_run(run_id) if run_id else None
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")

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
    # 아닌 종결 상태로 확정되면 그만큼 뺀다. recipient별로 각자 몫만 예약한다.
    for recipient_id, amount_minor in per_recipient_amounts(run).items():
        recipient = get_recipient(recipient_id)
        if recipient is not None:
            increment_recipient_monthly(recipient_id, amount_minor)

    try:
        enqueue_execute_payout(run_id)
        note = None
    except QueueNotConfigured as e:
        note = str(e)
    else:
        _try_enqueue_reconcile(run_id)

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


def _try_enqueue_reconcile(run_id: str) -> None:
    """execute-payout enqueue가 이미 성공한 뒤라 큐는 구성돼 있다고 보되, reconcile
    예약 자체가 실패해도 송금 흐름을 막지 않는다 — 감사 로그만 남긴다."""
    try:
        enqueue_reconcile(run_id)
    except QueueNotConfigured as e:
        record_audit_log(
            actor="api/src/payouts",
            action="RECONCILE_ENQUEUE_FAILED",
            run_id=run_id,
            reason=str(e),
        )


@router.post("/payouts/{run_id}/retry")
def retry_payout(
    run_id: str,
    x_approval_token: str | None = Header(default=None, alias="X-Approval-Token"),
):
    """schema-contract.md §8 재발송 — FAILED run을 새 승인 토큰으로 다시 보낸다.
    사람이 먼저 POST /settlements/runs/{run_id}/approve로 FAILED→APPROVED 재승인해
    새 토큰을 받아야 한다(재발송도 돈이 나가는 행위라 approve의 CAS·캡 검사를 다시 탄다).
    이 라우트는 그 토큰으로 APPROVED→EXECUTING만 담당한다 — verify_and_burn_token은
    상태가 APPROVED이기만 하면 최초 송금과 재발송을 구분하지 않는다.
    """
    run = get_settlement_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")

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

    # schema-contract.md §3 — retry_seq를 올려야 execute-payout이 §3 재발송 ID
    # 형식(build_payout_ids)으로 새 sender_batch_id를 만든다. 여기서 한 번만 올린다.
    retry_seq = run.get("retry_seq", 0) + 1
    update_settlement_run(run_id, {"retry_seq": retry_seq})

    for recipient_id, amount_minor in per_recipient_amounts(run).items():
        recipient = get_recipient(recipient_id)
        if recipient is not None:
            increment_recipient_monthly(recipient_id, amount_minor)

    try:
        enqueue_execute_payout(run_id)
        note = None
    except QueueNotConfigured as e:
        note = str(e)
    else:
        _try_enqueue_reconcile(run_id)

    record_audit_log(
        actor="api/src/payouts",
        action="PAYOUT_RETRY_ENQUEUED",
        run_id=run_id,
        after={"status": "EXECUTING", "retry_seq": retry_seq},
        reason=note,
    )

    response = {"settlement_run_id": run_id, "status": "EXECUTING", "retry_seq": retry_seq}
    if note:
        response["note"] = note
    return response


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

    recipient가 여럿이면 단일 PayPal 배치에 recipient당 item 하나씩 담아 한 번에 보낸다
    (sender_batch_id는 recipient와 무관하게 하나 — build_payout_ids 참조). 각 item의
    금액은 run 승인 시점에 고정된 fx_rates로 그 recipient 몫만 환산·합산한 값이다.
    """
    verify_oidc(authorization)
    run_id = body.get("settlement_run_id")
    run = get_settlement_run(run_id) if run_id else None
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    if run["status"] != "EXECUTING":
        raise HTTPException(
            status_code=409,
            detail=f"settlement_run status is {run['status']}, expected EXECUTING",
        )

    amounts = per_recipient_amounts(run)
    if not amounts:
        raise HTTPException(status_code=404, detail=f"settlement_run {run_id} has no linked claims")

    recipients = {}
    for recipient_id in amounts:
        recipient = get_recipient(recipient_id)
        if recipient is None:
            raise HTTPException(status_code=404, detail=f"unknown recipient_id: {recipient_id}")
        recipients[recipient_id] = recipient

    currency = os.environ["PAYOUT_CURRENCY"]
    try:
        assert_supported_payout_currency(currency)
    except UnsupportedPayoutCurrency:
        record_audit_log(
            actor="api/src/payouts",
            action="PAYOUT_CALL_FAILED",
            run_id=run_id,
            reason=f"currency not supported by PayPal Payouts: {currency}",
        )
        raise HTTPException(
            status_code=422, detail=f"currency not supported by PayPal Payouts: {currency}"
        )

    retry_seq = run.get("retry_seq", 0)
    sender_batch_id = build_payout_ids(run_id, next(iter(amounts)), retry_seq)[0]
    sender_item_ids = {
        recipient_id: build_payout_ids(run_id, recipient_id, retry_seq)[1] for recipient_id in amounts
    }
    paypal_values = {
        recipient_id: minor_to_paypal_value(amount_minor, currency)
        for recipient_id, amount_minor in amounts.items()
    }

    try:
        batch = create_payout(
            sender_batch_id,
            [
                {
                    "recipient_type": "EMAIL",
                    "amount": {"value": paypal_values[recipient_id], "currency": currency},
                    "receiver": recipients[recipient_id]["paypal_email"],
                    "sender_item_id": sender_item_ids[recipient_id],
                }
                for recipient_id in amounts
            ],
        )
    except http_requests.HTTPError as e:
        record_audit_log(
            actor="api/src/payouts", action="PAYOUT_CALL_FAILED", run_id=run_id, reason=str(e)
        )
        raise HTTPException(status_code=502, detail="PayPal payout call failed") from e

    payout_batch_id = batch.get("batch_header", {}).get("payout_batch_id")
    detail = get_payout_batch(payout_batch_id) if payout_batch_id else {}
    detail_by_sender_item_id = {
        i.get("payout_item", {}).get("sender_item_id"): i for i in detail.get("items", [])
    }

    now = datetime.now(UTC)
    sender_items = []
    for recipient_id, amount_minor in amounts.items():
        sender_item_id = sender_item_ids[recipient_id]
        item = detail_by_sender_item_id.get(sender_item_id, {})
        transaction_status = item.get("transaction_status", "PENDING")
        internal_status = (
            transaction_status
            if transaction_status in {"SUCCESS", "FAILED", "UNCLAIMED", "PENDING"}
            else "OTHER"
        )
        sender_items.append(
            {
                "sender_item_id": sender_item_id,
                "settlement_run_id": run_id,
                "recipient_id": recipient_id,
                "receiver_email": recipients[recipient_id]["paypal_email"],
                "amount_minor": amount_minor,
                "currency": currency,
                "paypal_value": paypal_values[recipient_id],
                "payout_item_id": item.get("payout_item_id"),
                "paypal_transaction_status": transaction_status,
                "status": internal_status,
                "retry_of": None,
                "created_at": now,
                "updated_at": now,
            }
        )
    set_sender_items(run_id, sender_items)
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
        "sender_items": sender_items,
    }


@router.post("/tasks/reconcile")
def task_reconcile(body: dict, authorization: str = Header(default="")):
    """schema-contract.md §8 — 주 경로. /payouts enqueue 시 RECONCILE_DELAY_SECONDS 뒤에
    Cloud Tasks가 부른다. 미종결(pending_items가 있고 아직 최대 시도 전)이면 이 라우트가
    스스로를 다시 예약한다 — reconcile()이 최대 시도(PAYOUT_MAX_RECONCILE_ATTEMPTS)를
    넘기면 강제로 종결 처리하므로, 그 안에서는 계속 폴링해도 된다."""
    verify_oidc(authorization)
    run_id = body.get("settlement_run_id")
    if not run_id:
        raise HTTPException(status_code=400, detail="settlement_run_id required")

    try:
        result = reconcile(run_id)
        if result.get("status") not in ("SETTLED", "FAILED"):
            _try_enqueue_reconcile(run_id)
        return result
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
