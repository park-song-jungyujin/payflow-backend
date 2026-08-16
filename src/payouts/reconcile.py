"""schema-contract.md §8 결과 대조 — `/tasks/reconcile`(주 경로, Cloud Tasks 지연 폴링)과
`/webhooks/paypal`(보조 경로)이 공유하는 핵심 로직. 돈 상태를 실제로 바꾸는 단 하나의
지점이다.

종결 판정(§8):
- 전 항목 SUCCESS → run SETTLED, claims SETTLED
- FAILED/UNCLAIMED/OTHER가 있음 → run FAILED, 성공분만 SETTLED, 나머지는
  CONFIRMED로 되돌리고 settlement_run_id를 비운다. 예약했던 monthly_paid_minor도 뺀다.

현재는 실행 단계(execute-payout)에서 이미 단일 recipient run만 통과시키므로, 여기서도
그 전제를 그대로 따른다 — monthly_paid_minor 역산이 run.total_amount_minor 하나로
끝난다.
"""

import os
from datetime import UTC, datetime

from ..guards.audit import record_audit_log
from .paypal_client import get_payout_batch
from .store import (
    get_claims_for_run,
    get_recipient,
    get_sender_items,
    get_settlement_run,
    increment_recipient_monthly,
    set_sender_items,
    update_claim,
    update_settlement_run,
)

_TERMINAL = {"SUCCESS", "FAILED", "UNCLAIMED", "OTHER"}
_KNOWN = {"SUCCESS", "FAILED", "UNCLAIMED", "PENDING"}


class RunNotFound(Exception):
    pass


class NotExecuting(Exception):
    def __init__(self, status: str):
        self.status = status


class MissingPayoutBatch(Exception):
    pass


def _now():
    return datetime.now(UTC)


def _refresh_items(run: dict) -> list[dict]:
    run_id = run["settlement_run_id"]
    payout_batch_id = run.get("payout_batch_id")
    items = get_sender_items(run_id)
    if not payout_batch_id or not items:
        raise MissingPayoutBatch(run_id)

    detail = get_payout_batch(payout_batch_id)
    latest_by_item_id = {i.get("payout_item_id"): i for i in detail.get("items", [])}

    updated = []
    for item in items:
        latest = latest_by_item_id.get(item.get("payout_item_id"))
        if latest is None:
            updated.append(item)
            continue
        status = latest.get("transaction_status", item["paypal_transaction_status"])
        updated.append(
            {
                **item,
                "paypal_transaction_status": status,
                "status": status if status in _KNOWN else "OTHER",
            }
        )
    set_sender_items(run_id, updated)
    return updated


def reconcile(run_id: str) -> dict:
    run = get_settlement_run(run_id)
    if run is None:
        raise RunNotFound(run_id)
    if run["status"] in ("SETTLED", "FAILED"):
        return {"settlement_run_id": run_id, "status": run["status"], "already_terminal": True}
    if run["status"] != "EXECUTING":
        raise NotExecuting(run["status"])

    items = _refresh_items(run)

    pending = [i for i in items if i["status"] not in _TERMINAL]
    if pending:
        attempts = run.get("reconcile_attempts", 0) + 1
        max_attempts = int(os.environ.get("PAYOUT_MAX_RECONCILE_ATTEMPTS", "5"))
        update_settlement_run(run_id, {"reconcile_attempts": attempts})
        if attempts < max_attempts:
            record_audit_log(
                actor="api/src/payouts",
                action="RECONCILE_PENDING",
                run_id=run_id,
                after={"reconcile_attempts": attempts, "pending_items": len(pending)},
            )
            return {
                "settlement_run_id": run_id,
                "status": run["status"],
                "pending_items": len(pending),
                "reconcile_attempts": attempts,
            }
        # 최대 시도 초과 — 더 기다리지 않고 남은 PENDING을 OTHER로 간주해 종결 처리로 넘어간다.
        items = [i if i["status"] in _TERMINAL else {**i, "status": "OTHER"} for i in items]
        set_sender_items(run_id, items)

    before_status = run["status"]
    all_success = all(i["status"] == "SUCCESS" for i in items)
    claims = get_claims_for_run(run_id)

    if all_success:
        for claim in claims:
            update_claim(claim["claim_id"], {"status": "SETTLED", "settled_at": _now()})
        new_status = "SETTLED"
    else:
        success_recipients = {i["recipient_id"] for i in items if i["status"] == "SUCCESS"}
        for claim in claims:
            if claim["recipient_id"] in success_recipients:
                update_claim(claim["claim_id"], {"status": "SETTLED", "settled_at": _now()})
            else:
                update_claim(claim["claim_id"], {"status": "CONFIRMED", "settlement_run_id": None})
        new_status = "FAILED"

        for recipient_id in {i["recipient_id"] for i in items if i["status"] != "SUCCESS"}:
            recipient = get_recipient(recipient_id)
            if recipient is None:
                continue
            already_paid = recipient.get("monthly_paid_minor", 0)
            increment_recipient_monthly(
                recipient_id, -min(already_paid, run["total_amount_minor"])
            )

    update_settlement_run(run_id, {"status": new_status})

    record_audit_log(
        actor="api/src/payouts",
        action="RUN_SETTLED" if new_status == "SETTLED" else "RUN_FAILED",
        run_id=run_id,
        before={"status": before_status},
        after={"status": new_status},
    )

    return {"settlement_run_id": run_id, "status": new_status, "sender_items": items}
