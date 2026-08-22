"""schema-contract.md §10 — POST /settlements/runs/{run_id}/approve.

money-safety.md 승인 게이트: (DRAFT | FAILED) → APPROVED CAS, 한도 캡 검사, 토큰 발급.
FAILED 허용은 schema-contract.md §8 "재발송" 때문 — 재발송도 새 승인 토큰이 있어야
하므로 이 엔드포인트를 그대로 재사용한다(payouts/routes.py retry_payout 참조).
Firestore 트랜잭션으로 상태를 재확인한 뒤 쓴다 — 동시에 두 번 승인되면
토큰이 두 개 발급되는 걸 막기 위해서다.
"""

import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import requests
from fastapi import APIRouter, Header, HTTPException
from google.cloud import firestore

from ..auth.session import verify_session
from ..payouts.currency import convert_minor
from ..payouts.fx import FxRateUnavailable, fetch_fx_rate
from ..payouts.store import get_client, get_claims_for_run, get_settlement_run
from .audit import record_audit_log
from .errors import GuardRejection
from .limits import check_caps
from .tokens import approval_amount_hash

router = APIRouter()


def _lock_fx_and_total(run: dict) -> tuple[dict[str, str], int]:
    """schema-contract.md §4 — 항목별 환산 후 합산. 환율은 승인 시점에 조회해 고정한다."""
    base_currency = os.environ["PAYOUT_CURRENCY"]
    claims = get_claims_for_run(run["settlement_run_id"])

    rates: dict[str, Decimal] = {}
    total_amount_minor = 0
    for claim in claims:
        currency = claim["currency"]
        if currency == base_currency:
            total_amount_minor += claim["amount_minor"]
            continue
        pair = f"{currency}/{base_currency}"
        if pair not in rates:
            try:
                rates[pair] = fetch_fx_rate(currency, base_currency)
            except (requests.RequestException, FxRateUnavailable) as e:
                raise HTTPException(
                    status_code=502, detail=f"fx rate lookup failed: {pair}"
                ) from e
        total_amount_minor += convert_minor(
            claim["amount_minor"], currency, base_currency, rates[pair]
        )

    return {pair: str(rate) for pair, rate in rates.items()}, total_amount_minor


@router.post("/settlements/runs/{run_id}/approve")
def approve_settlement_run(run_id: str, authorization: str = Header(default="")):
    session_token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
    session = verify_session(session_token)

    run = get_settlement_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")

    if run.get("org_id") != session["org_id"]:
        # 다른 기관의 run을 승인하지 못하게 막는다 — 404로 존재 자체를 숨긴다.
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")

    if run["status"] not in ("DRAFT", "FAILED"):
        raise HTTPException(
            status_code=409,
            detail=f"settlement_run status is {run['status']}, expected DRAFT or FAILED",
        )

    fx_rates, total_amount_minor = _lock_fx_and_total(run)
    if total_amount_minor <= 0:
        # 클레임이 하나도 안 걸린 run(예: FAILED 재승인 시점에 이미 다른 곳으로
        # 풀린 경우)을 조용히 0원으로 승인·토큰 발급하지 않는다.
        raise HTTPException(
            status_code=422, detail=f"settlement_run {run_id} has no linked claims to approve"
        )
    run = {**run, "fx_rates": fx_rates, "total_amount_minor": total_amount_minor}

    violation = check_caps(run)
    if violation:
        record_audit_log(
            org_id=session["org_id"],
            actor="api/src/guards",
            action="PAYOUT_REJECTED",
            run_id=run_id,
            reason=violation,
        )
        raise HTTPException(status_code=403, detail=violation)

    # 세션에서 검증된 신원으로 채운다 — 클라이언트가 보낸 값을 신뢰하지 않는다.
    approved_by = session["email"]
    token = secrets.token_urlsafe(32)
    ttl = int(os.environ.get("APPROVAL_TOKEN_TTL_SECONDS", "600"))
    now = datetime.now(UTC)

    updates = {
        "status": "APPROVED",
        "approved_by": approved_by,
        "approved_at": now,
        "fx_rates": fx_rates,
        "total_amount_minor": total_amount_minor,
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
        if current.get("status") not in ("DRAFT", "FAILED"):
            raise GuardRejection(
                409,
                f"settlement_run status is {current.get('status')}, expected DRAFT or FAILED",
            )
        transaction.update(run_ref, updates)

    try:
        _txn(client.transaction())
    except GuardRejection as rejection:
        raise HTTPException(status_code=rejection.status_code, detail=rejection.detail)

    record_audit_log(
        org_id=session["org_id"],
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
