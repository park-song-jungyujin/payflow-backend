"""schema-contract.md §10 — POST /settlements/runs/{run_id}/approve.

money-safety.md 승인 게이트: DRAFT → APPROVED CAS, 한도 캡 검사, 토큰 발급.
Firestore 트랜잭션으로 DRAFT 상태를 재확인한 뒤 쓴다 — 동시에 두 번 승인되면
토큰이 두 개 발급되는 걸 막기 위해서다.
"""

import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException
from google.cloud import firestore

from ..payouts.store import get_client, get_settlement_run
from .audit import record_audit_log
from .errors import GuardRejection
from .limits import check_caps
from .tokens import approval_amount_hash

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
    token = secrets.token_urlsafe(32)
    ttl = int(os.environ.get("APPROVAL_TOKEN_TTL_SECONDS", "600"))
    now = datetime.now(UTC)

    updates = {
        "status": "APPROVED",
        "approved_by": approved_by,
        "approved_at": now,
        "fx_locked_at": now,
        "updated_at": now,
        "approval_amount_hash": approval_amount_hash(run),
        "approval_token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "approval_token_expires_at": now + timedelta(seconds=ttl),
        "approval_token_used_at": None,
    }

    client = get_client()
    run_ref = client.collection("settlement_runs").document(run_id)

    @firestore.transactional
    def _txn(transaction):
        snapshot = run_ref.get(transaction=transaction)
        current = snapshot.to_dict() or {}
        if current.get("status") != "DRAFT":
            raise GuardRejection(
                409, f"settlement_run status is {current.get('status')}, expected DRAFT"
            )
        transaction.update(run_ref, updates)

    try:
        _txn(client.transaction())
    except GuardRejection as rejection:
        raise HTTPException(status_code=rejection.status_code, detail=rejection.detail)

    record_audit_log(
        actor=approved_by,
        actor_type="HUMAN",
        action="RUN_APPROVED",
        run_id=run_id,
        after={"status": "APPROVED"},
    )

    response = {**run, **updates}
    del response["approval_token_hash"]
    response["approval_token"] = token
    return response
