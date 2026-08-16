"""schema-contract.md §7 승인 토큰 — 발급 측과 검증 측이 같은 해시 함수를 부른다.

절대 규칙: 토큰 원문은 응답 본문으로 한 번만 나가고 저장은 해시만 한다. 여기 있는
어떤 함수도 원문을 반환값 밖으로 새어나가게 하지 않는다.
"""

import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta

from ..payouts.fixtures import get_claims_for_run
from .errors import GuardRejection
from .limits import check_caps


def approval_amount_hash(run: dict) -> str:
    items = [
        {
            "recipient_id": c["recipient_id"],
            "amount_minor": c["amount_minor"],
            "currency": c["currency"],
        }
        for c in sorted(get_claims_for_run(run["settlement_run_id"]), key=lambda c: c["recipient_id"])
    ]
    payload = {
        "settlement_run_id": run["settlement_run_id"],
        "base_currency": run["base_currency"],
        "fx_rates": dict(sorted(run.get("fx_rates", {}).items())),
        "items": items,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def issue_token(run: dict) -> str:
    """run을 in-place로 APPROVED 상태의 필드까지 채우고, 토큰 원문을 반환한다.
    이 반환값 이후로 원문은 어디에도 저장하지 않는다."""
    token = secrets.token_urlsafe(32)
    ttl = int(os.environ.get("APPROVAL_TOKEN_TTL_SECONDS", "600"))
    now = datetime.now(UTC)

    run["approval_amount_hash"] = approval_amount_hash(run)
    run["approval_token_hash"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
    run["approval_token_expires_at"] = (now + timedelta(seconds=ttl)).isoformat()
    run["approval_token_used_at"] = None

    return token


def verify_and_burn_token(run: dict, token: str | None) -> None:
    """실패하면 GuardRejection을 던진다. 성공하면 run을 EXECUTING으로 전이하고
    approval_token_used_at을 기록한다(소각) — 재사용은 여기서 막힌다."""
    if not token:
        raise GuardRejection(403, "X-Approval-Token header missing")

    if run.get("status") != "APPROVED":
        raise GuardRejection(
            409, f"settlement_run status is {run.get('status')}, expected APPROVED"
        )

    if run.get("approval_token_used_at") is not None:
        raise GuardRejection(403, "approval token already used")

    expires_at = run.get("approval_token_expires_at")
    if expires_at and datetime.fromisoformat(expires_at) < datetime.now(UTC):
        raise GuardRejection(403, "approval token expired")

    if hashlib.sha256(token.encode("utf-8")).hexdigest() != run.get("approval_token_hash"):
        raise GuardRejection(403, "invalid approval token")

    if approval_amount_hash(run) != run.get("approval_amount_hash"):
        raise GuardRejection(403, "approval_amount_hash mismatch — run contents changed after approval")

    violation = check_caps(run)
    if violation:
        raise GuardRejection(403, violation)

    run["approval_token_used_at"] = datetime.now(UTC).isoformat()
    run["status"] = "EXECUTING"
