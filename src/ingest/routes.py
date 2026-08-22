"""schema-contract.md §10 — POST /slack/events (A 소유).

architecture.md §비동기: 서명검증 → Firestore raw 저장 → enqueue → 200, 목표 0.5s.
**3초 안에 200을 돌려주는 게 이 라우트의 유일한 성능 요구다.** 파일 다운로드,
GCS 업로드, Gemini 호출은 전부 파싱 태스크 몫이다 — 여기서 하면 3초를 넘긴다.

Slack은 ack가 늦으면 같은 이벤트를 최대 3회 재전송한다. dedup은 store.py가
slack_file_id로 하고, 이 라우트는 재전송이어도 enqueue는 다시 한다 — 파싱 태스크는
같은 receipt_id를 덮어쓰므로 멱등이고, 앞 요청이 enqueue 직전에 죽었을 수 있다.
"""

import logging
import os
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request

from ..guards.audit import record_audit_log
from ..guards.oidc import verify_oidc
from ..parsing.store import get_receipt
from ..payouts.store import get_recipient
from ..settlements.store import get_agent_draft
from .drafts import InvalidDraftPayload, parse_claimant_payload
from .enqueue import QueueNotConfigured, enqueue_parse_receipt, enqueue_remind
from .reminders import ReminderAction, decide, parse_expires_at
from .signature import SignatureError, verify_slack_signature
from .slack_client import SlackSendPermanent, SlackSendTransient, post_message
from .store import (
    ReceiptStoreUnavailable,
    apply_claimant_verdict,
    claim_send_slot,
    create_receipt_if_absent,
    find_recipient_by_slack_user,
    get_claim_request,
    mark_expired,
    record_sent,
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

    # 커밋은 이미 끝났다 — 이 감사 로그가 던져도 500으로 뒤집으면 안 된다.
    # 재시도하면 receipt는 이미 전이된 상태라 SKIPPED로 빠지고 "APPLIED" 기록이
    # 영영 안 남는다. store.py의 CLAIM_DEMOTION_BLOCKED(_AUDIT_FAILED)와 같은
    # 이중 폴백 — 재기록도 실패하면 logging으로 흔적만 남긴다.
    try:
        record_audit_log(
            actor="api/src/ingest",
            action="CLAIMANT_DRAFT_APPLIED",
            after={"receipt_id": receipt_id, "task_id": task_id, "result": result},
        )
    except Exception as e:
        try:
            record_audit_log(
                actor="api/src/ingest",
                action="CLAIMANT_DRAFT_APPLIED_AUDIT_FAILED",
                reason=str(e),
                after={"receipt_id": receipt_id, "task_id": task_id, "result": result},
            )
        except Exception:
            logging.getLogger(__name__).error(
                "CLAIMANT_DRAFT_APPLIED audit log failed twice for receipt %s (task %s, result %s): %s",
                receipt_id,
                task_id,
                result,
                e,
            )

    return {"status": "ok", "receipt_id": receipt_id, "result": result}


def _audit_best_effort(**kwargs) -> None:
    """이미 커밋(또는 발송)이 끝난 뒤의 감사 로그. 던져도 요청을 500으로 뒤집으면
    안 된다 — 재시도는 CAS·상태 가드에 걸려 같은 자리에서 다시 죽을 뿐이고,
    이미 나간 DM은 되돌아오지 않는다. 위 CLAIMANT_DRAFT_APPLIED와 같은 판단이다."""
    try:
        record_audit_log(**kwargs)
    except Exception as e:
        logging.getLogger(__name__).error(
            "audit log failed (%s): %s", kwargs.get("action"), e
        )


def _requery_message(receipt_id: str | None) -> str | None:
    """DM 본문은 청구자 에이전트가 쓴 `requery_message`에서만 온다. 없으면 None —
    **코드가 대체 문장을 만들지 않는다.** 문안을 여기서 지어내면 "에이전트가 뭐라고
    했는지"의 기록이 사라지고, 사람은 에이전트가 판단한 적 없는 이유로 재요청을 받는다.

    draft 읽기는 settlements/store.py의 get_agent_draft(agent_drafts의 유일한
    읽기)를 그대로 쓴다 — task_apply_claimant_draft와 같은 경로다.
    """
    if not receipt_id:
        return None
    draft = get_agent_draft(f"CLAIMANT:{receipt_id}")
    payload = (draft or {}).get("payload")
    if not isinstance(payload, dict):
        return None
    message = payload.get("requery_message")
    # 에이전트 출력은 비신뢰 입력이다 — str이 아닐 수도 있다(drafts.py와 같은 전제).
    if not isinstance(message, str) or not message.strip():
        return None
    return message


def _resolve_target(claim_request: dict, receipt_id: str | None) -> tuple[str, str | None] | None:
    """(channel, thread_ts)를 정한다. 보낼 곳이 없으면 None.

    schema-contract.md §2 — receipts의 `slack_channel_id`·`slack_message_ts`는
    "같이 쓰거나 같이 비운다". 그래서 **둘 다 있을 때만** 원본 업로드 스레드에
    답글을 단다. 한쪽만 있는 문서는 계약 밖이고, channel 없이 thread_ts만으로는
    발송 자체가 불가능하므로 DM으로 내려간다.

    DM일 때 thread_ts는 최초 DM의 ts(`slack_dm_ts`)다 — 재촉이 최초 DM 아래
    붙어야 사람이 무엇에 대한 재촉인지 알 수 있다(store.record_sent 주석 참고).
    """
    receipt = get_receipt(receipt_id) if receipt_id else None
    if receipt:
        channel = receipt.get("slack_channel_id")
        message_ts = receipt.get("slack_message_ts")
        if channel and message_ts:
            return channel, message_ts

    recipient = get_recipient(claim_request.get("recipient_id")) or {}
    slack_user_id = recipient.get("slack_user_id")
    if not slack_user_id:
        return None
    return slack_user_id, claim_request.get("slack_dm_ts")


def _next_delay_seconds(action: ReminderAction, claim_request: dict, now: datetime) -> int | None:
    """다음 깨어남까지의 초. 예약하지 않아야 하면 None.

    최초 발송 뒤에는 REMINDER_DELAY_SECONDS 뒤에 재촉을 깨우고, 재촉을 보낸
    뒤에는 `expires_at`에 맞춰 만료 태스크를 깨운다. 만료 시각을 모르면(파싱
    실패·부재) 예약하지 않는다 — decide()가 그 경우 SKIP을 내는 것과 같은
    판단이다. 추측한 시각으로 예약하면 아직 유효한 건이 일찍 만료된다.
    """
    if action == ReminderAction.SEND_INITIAL:
        # schema-contract.md §11 — 데모 20초, 실제 86400초.
        return int(os.environ.get("REMINDER_DELAY_SECONDS", "20"))

    expires_at = parse_expires_at(claim_request.get("expires_at"))
    if expires_at is None:
        return None
    remaining = int((expires_at - now).total_seconds())
    return None if remaining < 0 else remaining


def _try_enqueue_remind(claim_request_id: str, delay_seconds: int) -> None:
    """enqueue 실패는 삼킨다. 상태 전이도 Slack 발송도 이미 끝난 뒤라 여기서
    500을 내면 재시도가 CAS에 걸려 아무 일도 못 하고 같은 자리에서 다시 죽는다.
    parsing/pipeline.py의 CLAIMANT_ENQUEUE_FAILED와 같은 형태다."""
    try:
        enqueue_remind(claim_request_id, delay_seconds=delay_seconds)
    except Exception as e:
        _audit_best_effort(
            actor="api/src/ingest",
            action="REMIND_ENQUEUE_FAILED",
            reason=str(e),
            after={"claim_request_id": claim_request_id, "delay_seconds": delay_seconds},
        )


@router.post("/tasks/remind")
def task_remind(body: dict, authorization: str = Header(default="")):
    """schema-contract.md §10 — 재촉 루프의 한 번의 깨어남. Cloud Tasks가 부른다.

    **HTTP 코드가 큐의 재시도 손잡이다.** 다시 부르면 될 실패(Slack 5xx·429,
    Firestore 트랜잭션 소진)만 503으로 올리고, 다시 불러도 같을 실패(문안 부재,
    Slack의 논리 오류)는 200 + 감사 로그로 끝낸다 — 재시도해도 결과가 같은데
    큐를 계속 돌리면 백오프만 쌓인다.

    순서는 "CAS → 발송 → 기록 → 재예약"이다. 미리 표시해두면 발송 실패가 조용한
    유실이 되고(store.claim_send_slot 주석), 재예약을 먼저 하면 발송 실패 시
    같은 태스크가 두 갈래로 늘어난다.
    """
    verify_oidc(authorization)

    claim_request_id = body.get("claim_request_id")
    if not claim_request_id:
        raise HTTPException(status_code=400, detail="claim_request_id required")

    claim_request = get_claim_request(claim_request_id)
    if claim_request is None:
        raise HTTPException(
            status_code=404, detail=f"unknown claim_request_id: {claim_request_id}"
        )

    now = datetime.now(UTC)
    action = decide(claim_request, now=now)

    try:
        if action == ReminderAction.SKIP:
            return {"status": "ok", "action": action.value}

        if action == ReminderAction.EXPIRE:
            # mark_expired는 claim_send_slot과 달리 decide()를 재확인하지 않는다.
            # 여기서 한 번 더 감싸지 않는 이유: 만료 판단이 보는 필드는
            # status와 expires_at 둘뿐인데, status는 mark_expired가 트랜잭션
            # 안에서 PENDING·REMINDED로 직접 가드하고 expires_at은 생성 시점
            # 이후 아무도 쓰지 않는다. **expires_at을 연장하는 기능이 생기면
            # 이 가정이 깨지므로 그때 mark_expired에 decide() 재확인을 넣어야 한다.**
            mark_expired(claim_request_id, now=now)
            _audit_best_effort(
                actor="api/src/ingest",
                action="CLAIM_REQUEST_EXPIRED",
                after={"claim_request_id": claim_request_id, "status": "EXPIRED"},
            )
            return {"status": "ok", "action": action.value}

        receipt_id = claim_request.get("receipt_id")

        message = _requery_message(receipt_id)
        if message is None:
            # 문안이 없으면 보내지 않는다. 재시도해도 draft는 그대로다.
            record_audit_log(
                actor="api/src/ingest",
                action="CLAIM_REQUEST_NO_MESSAGE",
                reason=f"agent_drafts/CLAIMANT:{receipt_id}에 requery_message가 없다",
                after={"claim_request_id": claim_request_id, "receipt_id": receipt_id},
            )
            return {"status": "ignored", "reason": "no_message"}

        target = _resolve_target(claim_request, receipt_id)
        if target is None:
            record_audit_log(
                actor="api/src/ingest",
                action="CLAIM_REQUEST_NO_TARGET",
                reason=f"recipient {claim_request.get('recipient_id')}에 slack_user_id가 없다",
                after={"claim_request_id": claim_request_id, "receipt_id": receipt_id},
            )
            return {"status": "ignored", "reason": "no_target"}
        channel, thread_ts = target

        # 발송 직전 CAS. 태스크가 중복 전달되면 같은 DM이 두 번 나간다.
        if not claim_send_slot(claim_request_id, expected_action=action, now=now):
            return {"status": "ignored", "reason": "slot_taken"}

        try:
            slack_ts = post_message(channel=channel, text=message, thread_ts=thread_ts)
        except SlackSendTransient as e:
            # 아직 아무것도 기록하지 않았다 — 재시도가 처음부터 다시 돈다.
            raise HTTPException(status_code=503, detail=f"slack send failed: {e}")
        except SlackSendPermanent as e:
            _audit_best_effort(
                actor="api/src/ingest",
                action="CLAIM_REQUEST_SEND_FAILED",
                reason=str(e),
                after={"claim_request_id": claim_request_id, "channel": channel},
            )
            return {"status": "ignored", "reason": "send_failed"}

        record_sent(claim_request_id, slack_ts=slack_ts, action=action, now=now)
    except ReceiptStoreUnavailable as e:
        # store.py 도큐스트링 — 호출부가 503으로 바꾼다. 트랜잭션이 원자적이라
        # 재시도해도 안전하다.
        record_audit_log(
            actor="api/src/ingest",
            action="CLAIM_REQUEST_STORE_UNAVAILABLE",
            reason=f"firestore transaction failed: {e}",
            after={"claim_request_id": claim_request_id, "action": action.value},
        )
        raise HTTPException(status_code=503, detail="claim request store unavailable")

    _audit_best_effort(
        actor="api/src/ingest",
        action="CLAIM_REQUEST_SENT",
        after={
            "claim_request_id": claim_request_id,
            "action": action.value,
            "slack_ts": slack_ts,
        },
    )

    delay_seconds = _next_delay_seconds(action, claim_request, now)
    if delay_seconds is not None:
        _try_enqueue_remind(claim_request_id, delay_seconds)

    return {"status": "ok", "action": action.value, "slack_ts": slack_ts}
