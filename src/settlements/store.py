"""schema-contract.md §2 "검증" — B가 쓰는 receipts/claim_requests 창구.

`receipts` 생성은 A 소유(`ingest/store.py`)지만, 검증 필드(verified_at·
verification_signals·status 전이)는 §2 "검증" 절이 명시적으로 B(`POST
/settlements/runs`) 몫으로 지정한다. `claim_requests`도 마찬가지다 — B가 쓰고
DM 발송은 A가 소유한 기존 재촉 루프가 그대로 탄다. `get_client`만 재사용한다.

`get_agent_draft`는 `agent_drafts`의 유일한 읽기다 — 쓰기는 `guards/agent_drafts.py`
(`POST /agents/drafts`)가 전담하고, B는 정산 실행 응답에 실어 보낼 때만 읽는다.
"""

import os
from datetime import UTC, datetime, timedelta

from ulid import ULID

from ..payouts.store import get_client
from ..schemas.models import VerificationSignals


def get_receipts(receipt_ids: set[str]) -> dict[str, dict]:
    """존재하지 않는 receipt_id는 결과에서 빠진다 — 호출부가 이를 "판정 불가"로
    취급한다(verification.verify_candidates)."""
    result = {}
    for rid in receipt_ids:
        snapshot = get_client().collection("receipts").document(rid).get()
        if snapshot.exists:
            result[rid] = snapshot.to_dict()
    return result


def save_verification_result(receipt_id: str, *, passed: bool, signals: VerificationSignals) -> None:
    """통과든 실패든 verified_at을 찍는다 — "검증을 이미 시도했다"는 캐시 플래그이지
    "통과했다"는 뜻이 아니다. 실패 시에만 status가 VERIFICATION_FAILED로 바뀐다."""
    now = datetime.now(UTC)
    get_client().collection("receipts").document(receipt_id).update(
        {
            "verified_at": now,
            "verification_signals": signals.model_dump(mode="json"),
            "status": "PARSED" if passed else "VERIFICATION_FAILED",
            "updated_at": now,
        }
    )


def create_verification_failed_claim_request(*, recipient_id: str, receipt_id: str) -> str:
    """schema-contract.md §2 claim_requests reason — VERIFICATION_FAILED는
    receipt_id가 필수다.

    CLAIM_REQUEST_TTL_SECONDS 기본값 604800초(7일)는 A의 재촉 루프가 아직
    없어 실제 선례가 없는 임시값이다 — A가 /tasks/remind를 구현할 때 여기
    기본값과 맞춰야 한다."""
    ttl_seconds = int(os.environ.get("CLAIM_REQUEST_TTL_SECONDS", "604800"))
    now = datetime.now(UTC)
    claim_request_id = f"crq_{ULID()}"
    get_client().collection("claim_requests").document(claim_request_id).set(
        {
            "claim_request_id": claim_request_id,
            "recipient_id": recipient_id,
            "receipt_id": receipt_id,
            "reason": "VERIFICATION_FAILED",
            "slack_dm_ts": None,
            "reminded_at": None,
            "expires_at": now + timedelta(seconds=ttl_seconds),
            "status": "PENDING",
            "created_at": now,
            "updated_at": now,
        }
    )
    return claim_request_id


def get_agent_draft(task_id: str) -> dict | None:
    """agent_drafts 문서 ID는 task_id다(guards/agent_drafts.py). 존재하지 않으면
    None — 아직 분석이 안 끝났거나 enqueue 자체가 실패한 경우다. 호출부가
    "분석 대기 중"과 "이상 없음(anomalies=[])"을 구분해야 하므로 조용히 빈 dict를
    대신 돌려주지 않는다."""
    doc = get_client().collection("agent_drafts").document(task_id).get()
    return doc.to_dict() if doc.exists else None
