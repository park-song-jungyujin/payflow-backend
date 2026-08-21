"""schema-contract.md §10 — POST /slack/events (A 소유).

architecture.md §비동기: 서명검증 → Firestore raw 저장 → enqueue → 200, 목표 0.5s.
**3초 안에 200을 돌려주는 게 이 라우트의 유일한 성능 요구다.** 파일 다운로드,
GCS 업로드, Gemini 호출은 전부 파싱 태스크 몫이다 — 여기서 하면 3초를 넘긴다.

Slack은 ack가 늦으면 같은 이벤트를 최대 3회 재전송한다. dedup은 store.py가
slack_file_id로 하고, 이 라우트는 재전송이어도 enqueue는 다시 한다 — 파싱 태스크는
같은 receipt_id를 덮어쓰므로 멱등이고, 앞 요청이 enqueue 직전에 죽었을 수 있다.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request

from ..guards.audit import record_audit_log
from ..guards.oidc import verify_oidc
from ..settlements.store import get_agent_draft
from .drafts import InvalidDraftPayload, parse_claimant_payload
from .enqueue import QueueNotConfigured, enqueue_parse_receipt
from .signature import SignatureError, verify_slack_signature
from .store import (
    ReceiptStoreUnavailable,
    apply_claimant_verdict,
    create_receipt_if_absent,
    find_recipient_by_slack_user,
)

router = APIRouter()

_IMAGE_MIMETYPES = {"image/jpeg", "image/png", "image/heic", "image/webp"}

# 파일마다 Firestore 트랜잭션 + Cloud Tasks enqueue를 순차로 돈다. 라우트 자체
# 오버헤드는 ~3ms로 무의미하고 비용은 전부 네트워크이며 파일 수에 선형이다.
# 실측(지연 주입): 파일당 왕복 400ms 가정에서 5장 2.0s, 8장 3.2s로 3초를 넘긴다.
# 5로 두면 비관적 케이스에서도 32% 여유가 남는다. 실제 업로드는 1~3장이다.
#
# 초과분은 버리지 않는다 — ack는 즉시 주고 처리 못 한 file_id를 감사 로그에
# 남긴다. 영수증이 조용히 사라지는 게 3초 초과보다 나쁘다.
MAX_FILES_PER_EVENT = 5


@router.post("/slack/events")
async def slack_events(request: Request):
    # 서명은 raw body 기준이다. request.json()을 먼저 부르면 안 된다.
    raw_body = await request.body()
    try:
        verify_slack_signature(
            raw_body,
            request.headers.get("X-Slack-Request-Timestamp", ""),
            request.headers.get("X-Slack-Signature", ""),
        )
    except SignatureError as e:
        raise HTTPException(status_code=401, detail=str(e))

    payload = await request.json()

    # 앱 설정에서 Event Subscriptions URL을 등록할 때 오는 핸드셰이크.
    # 이걸 에코하지 않으면 Slack 앱 등록 자체가 안 된다.
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    event = payload.get("event", {})
    if event.get("type") != "message" or event.get("bot_id"):
        return {"status": "ignored"}

    files = [f for f in event.get("files", []) if f.get("mimetype") in _IMAGE_MIMETYPES]
    if not files:
        return {"status": "ignored"}

    recipient = find_recipient_by_slack_user(event.get("user", ""))
    if recipient is None:
        # 조용히 버리지 않는다. 안내 DM은 재촉 루프(범위 밖)가 붙을 때 함께 단다.
        record_audit_log(
            actor="api/src/ingest",
            action="RECEIPT_INGEST_SKIPPED",
            reason=f"unregistered slack_user_id: {event.get('user')}",
        )
        return {"status": "ignored", "reason": "unregistered_user"}

    deferred = [f["id"] for f in files[MAX_FILES_PER_EVENT:]]
    if deferred:
        record_audit_log(
            actor="api/src/ingest",
            action="RECEIPT_INGEST_DEFERRED",
            reason=f"file count {len(files)} exceeds MAX_FILES_PER_EVENT={MAX_FILES_PER_EVENT}",
            after={"deferred_slack_file_ids": deferred},
        )
    files = files[:MAX_FILES_PER_EVENT]

    received = []
    for slack_file in files:
        try:
            receipt_id, created = create_receipt_if_absent(
                recipient_id=recipient["recipient_id"],
                slack_file_id=slack_file["id"],
                slack_channel_id=event["channel"],
                slack_message_ts=event["ts"],
            )
        except ReceiptStoreUnavailable as e:
            # 트랜잭션 ABORTED가 SDK 재시도 상한(기본 5회)을 넘긴 경우. store.py가
            # SDK의 ValueError를 이 도메인 예외로 바꿔 올린다 — 여기서 ValueError를
            # 직접 잡으면 무관한 ValueError까지 503으로 뭉뚱그린다.
            # enqueue 실패와 달리 receipts 문서가 남지 않았으므로 200으로 삼키면
            # 영수증이 조용히 사라진다. Slack 재전송을 받아야 맞다 — 다만 스택
            # 트레이스 500이 아니라 명시적 503으로 남긴다.
            record_audit_log(
                actor="api/src/ingest",
                action="RECEIPT_INGEST_FAILED",
                reason=f"firestore transaction failed: {e}",
            )
            raise HTTPException(status_code=503, detail="receipt store unavailable")
        if created:
            record_audit_log(
                actor="api/src/ingest",
                action="RECEIPT_RECEIVED",
                after={"receipt_id": receipt_id, "status": "RECEIVED"},
            )
        try:
            enqueue_parse_receipt(receipt_id)
        except QueueNotConfigured as e:
            # 3초 ack가 우선이다. 여기서 500을 내면 Slack이 재전송하는데 큐는
            # 여전히 없다. 문서는 남았으니 수동 재개가 가능하다.
            record_audit_log(
                actor="api/src/ingest",
                action="PARSE_ENQUEUE_FAILED",
                reason=str(e),
                after={"receipt_id": receipt_id},
            )
        received.append(receipt_id)

    return {"status": "ok", "receipt_ids": received}


@router.post("/tasks/apply-claimant-draft")
def task_apply_claimant_draft(body: dict, authorization: str = Header(default="")):
    """schema-contract.md §9 — 청구자 에이전트 draft를 상태 전이로 반영하는
    Cloud Tasks 타겟. draft 읽기는 settlements/store.py의 get_agent_draft
    (agent_drafts의 유일한 읽기)를 그대로 쓴다 — 여기서 새로 읽지 않는다.

    payload가 §9 계약과 형식적으로 안 맞으면(InvalidDraftPayload) 200 +
    감사 로그로 끝낸다 — 재시도해도 같은 payload라 큐를 계속 돌릴 이유가 없다.
    """
    verify_oidc(authorization)

    task_id = body.get("task_id")
    if not task_id:
        raise HTTPException(status_code=400, detail="task_id required")

    draft = get_agent_draft(task_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"unknown task_id: {task_id}")
    if draft.get("agent") != "CLAIMANT":
        raise HTTPException(
            status_code=400, detail=f"draft agent is {draft.get('agent')}, expected CLAIMANT"
        )
    if draft.get("target_type") != "RECEIPT":
        raise HTTPException(
            status_code=400,
            detail=f"draft target_type is {draft.get('target_type')}, expected RECEIPT",
        )

    receipt_id = draft.get("target_id")

    try:
        verdict = parse_claimant_payload(draft.get("payload") or {})
    except InvalidDraftPayload as e:
        record_audit_log(
            actor="api/src/ingest",
            action="CLAIMANT_DRAFT_APPLY_INVALID_PAYLOAD",
            reason=str(e),
            after={"receipt_id": receipt_id, "task_id": task_id},
        )
        return {"status": "ignored", "reason": "invalid_payload"}

    try:
        result = apply_claimant_verdict(receipt_id, verdict, now=datetime.now(UTC))
    except ReceiptStoreUnavailable as e:
        # store.py 도큐스트링 — 호출부가 503으로 바꾼다. slack_events와 같은 판단:
        # 트랜잭션이 원자적이라 재시도해도 안전하다(실패 시 PARSED로 남거나, 커밋은
        # 됐는데 응답만 유실된 경우엔 재실행이 CAS 가드에 걸려 SKIPPED로 빠진다).
        record_audit_log(
            actor="api/src/ingest",
            action="CLAIMANT_DRAFT_APPLY_FAILED",
            reason=f"firestore transaction failed: {e}",
            after={"receipt_id": receipt_id, "task_id": task_id},
        )
        raise HTTPException(status_code=503, detail="receipt store unavailable")

    record_audit_log(
        actor="api/src/ingest",
        action="CLAIMANT_DRAFT_APPLIED",
        after={"receipt_id": receipt_id, "task_id": task_id, "result": result},
    )

    return {"status": "ok", "receipt_id": receipt_id, "result": result}
