"""schema-contract.md §2 "검증" — B가 쓰는 receipts/claim_requests 창구.

`receipts` 생성은 A 소유(`ingest/store.py`)지만, 검증 필드(verified_at·
verification_signals·status 전이)는 §2 "검증" 절이 명시적으로 B(`POST
/settlements/runs`) 몫으로 지정한다. `claim_requests`도 마찬가지다 — B가 쓰고
DM 발송은 A가 소유한 기존 재촉 루프가 그대로 탄다. `get_client`만 재사용한다.

`get_agent_draft`는 `agent_drafts`의 유일한 읽기다 — 쓰기는 `guards/agent_drafts.py`
(`POST /agents/drafts`)가 전담하고, B는 정산 실행 응답에 실어 보낼 때만 읽는다.
예외로 `set_executor_analysis_status`는 에이전트가 아직 분석을 시작/완료하기 전
임시 상태(PROCESSING/FAILED)를 정산 실행 생성 시점에 backend가 직접 쓴다 — 이건
에이전트 호출이 아니라 routes.py가 같은 요청 안에서 동기로 쓰는 것이라 OIDC 게이트를
탈 필요가 없다.
"""

import os
from datetime import UTC, datetime, timedelta

from ulid import ULID

from ..payouts.store import get_client
from ..schemas.models import VerificationSignals
from .enqueue import executor_draft_task_id


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


def create_verification_failed_claim_request(*, org_id: str, recipient_id: str, receipt_id: str) -> str:
    """schema-contract.md §2 claim_requests reason — VERIFICATION_FAILED는
    receipt_id가 필수다.

    CLAIM_REQUEST_TTL_SECONDS 기본값은 86400초(1일)다 — src/ingest/store.py와
    동일하고, tests/fixtures/02_parse_failure_requery.json의 claim_requests가
    expires_at = created_at + 1일로 저장돼 있어 공유 fixture상 유일한 실제
    선례다."""
    ttl_seconds = int(os.environ.get("CLAIM_REQUEST_TTL_SECONDS", "86400"))
    now = datetime.now(UTC)
    claim_request_id = f"crq_{ULID()}"
    get_client().collection("claim_requests").document(claim_request_id).set(
        {
            "claim_request_id": claim_request_id,
            "org_id": org_id,
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


def set_executor_analysis_status(run_id: str, status: str, reason: str | None = None) -> None:
    """정산 실행 생성 시점에 backend가 직접 agent_drafts에 쓰는 임시 상태(PROCESSING/
    FAILED) — 에이전트가 submit_settlement_analysis로 최종 결과를 쓰면 같은 문서를
    통째로 덮어써 status 필드가 사라진다(guards/agent_drafts.py write_agent_draft).
    routes._executor_analysis가 payload에 status가 없으면 "DONE"으로 기본값 처리하는
    이유가 이것 — 기존에 쓰인 draft(에이전트가 이미 완료한 분석)와 호환된다."""
    task_id = executor_draft_task_id(run_id)
    get_client().collection("agent_drafts").document(task_id).set(
        {
            "draft_id": f"drf_{task_id}",
            "agent": "EXECUTOR",
            "target_type": "SETTLEMENT_RUN",
            "target_id": run_id,
            "task_id": task_id,
            "payload": {"status": status, **({"reason": reason} if reason else {})},
            "created_at": datetime.now(UTC),
        }
    )
