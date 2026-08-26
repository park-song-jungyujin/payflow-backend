"""schema-contract.md §10 — POST /slack/events · /slack/interactions (A 소유).

architecture.md §비동기: 서명검증 → Firestore raw 저장 → enqueue → 200, 목표 0.5s.
**3초 안에 200을 돌려주는 게 이 라우트의 유일한 성능 요구다.** 파일 다운로드,
GCS 업로드, Gemini 호출은 전부 파싱 태스크 몫이다 — 여기서 하면 3초를 넘긴다.

Slack은 ack가 늦으면 같은 이벤트를 최대 3회 재전송한다. dedup은 store.py가
slack_file_id로 하고, 이 라우트는 재전송이어도 enqueue는 다시 한다 — 파싱 태스크는
같은 receipt_id를 덮어쓰므로 멱등이고, 앞 요청이 enqueue 직전에 죽었을 수 있다.
"""

import json
import logging
import math
import os
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs

from fastapi import APIRouter, Header, HTTPException, Request

from ..auth.store import get_or_create_default_org_id, get_slack_workspace_by_team
from ..guards.audit import record_audit_log
from ..guards.oidc import verify_oidc
from ..guards.translate import translate_lines
from ..parsing.store import get_receipt
from ..payouts.currency import CURRENCY_EXPONENT
from ..payouts.store import get_claims_for_run, get_recipient
from ..settlements.store import get_agent_draft, get_receipts
from .drafts import InvalidDraftPayload, parse_claimant_payload
from .enqueue import QueueNotConfigured, enqueue_parse_receipt, enqueue_remind
from .reminders import ReminderAction, decide, parse_expires_at
from .signature import SignatureError, verify_slack_signature
from .slack_client import (
    CLAIM_REQUEST_ACTION_ID,
    SlackSendError,
    SlackSendPermanent,
    SlackSendTransient,
    get_display_name,
    get_user_locale,
    post_message,
    requery_blocks,
)
from .store import (
    CLAIM_REQUEST_TTL_SECONDS,
    ReceiptStoreUnavailable,
    apply_claimant_verdict,
    claim_send_slot,
    create_receipt_if_absent,
    create_recipient_from_slack,
    find_recipient_by_slack_user,
    get_claim_request,
    mark_expired,
    mark_responded,
    record_sent,
)

router = APIRouter()

# RFC 5322 전체를 흉내내지 않는다 — Slack 메시지 텍스트에서 사람이 실제로 칠 법한
# 이메일 하나를 건지는 용도다. 오탐(진짜 이메일이 아닌데 매칭)보다 누락이 안전하다
# — create_recipient_from_slack이 verified=False로 시작해 관리자 확인을 거친다.
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@(?:[\w-]+\.)+[a-zA-Z]{2,}")


def _extract_email(text: str) -> str | None:
    match = _EMAIL_PATTERN.search(text)
    return match.group(0) if match else None

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

    team_id = payload.get("team_id", "")
    workspace = get_slack_workspace_by_team(team_id)
    # 로그인 게이트를 뗀 것과 같은 이유·같은 방식(guards/routes.py,
    # settlements/routes.py) — 이 team_id로 정식 설치(/auth/slack/callback)를
    # 아직 아무도 안 거쳤어도, 서명(앱 전체 단위 시크릿)은 이미 통과했으니
    # 기본 기관으로 자동 연결한다. 진짜 멀티테넌시가 필요해지면 이 폴백을
    # 떼고 정식 설치를 다시 강제하면 된다.
    if workspace is None:
        record_audit_log(
            actor="api/src/ingest",
            action="RECEIPT_INGEST_AUTO_LINKED",
            reason=f"unregistered slack team_id: {team_id}, falling back to default org",
        )
    org_id = workspace["org_id"] if workspace else get_or_create_default_org_id()

    slack_user_id = event.get("user", "")
    # 미등록 사용자 셀프 등록 — 파일 유무와 무관하게 먼저 확인한다. 텍스트만
    # 있는 이메일 답장도 여기서 잡아야 한다(files 필터 뒤에 두면 텍스트만 온
    # 메시지가 그 전에 이미 "ignored"로 버려진다).
    recipient = find_recipient_by_slack_user(org_id, slack_user_id)
    if recipient is None:
        email = _extract_email(event.get("text", ""))
        if email:
            create_recipient_from_slack(
                org_id=org_id,
                slack_user_id=slack_user_id,
                paypal_email=email,
                display_name=get_display_name(slack_user_id),
            )
            try:
                post_message(
                    channel=event["channel"],
                    text=(
                        f"연결됐습니다! 앞으로 이 채널로 보내는 영수증이 자동으로 처리됩니다."
                        f" (등록된 PayPal 이메일: {email})"
                    ),
                )
            except SlackSendError:
                # 등록(Firestore 쓰기)은 이미 끝났다 — 안내 DM 실패로 되돌리지 않는다.
                pass
            return {"status": "ok", "reason": "recipient_registered"}

        # 조용히 버리지 않는다. 이메일이 아니면 등록 방법을 안내한다.
        record_audit_log(
            actor="api/src/ingest",
            action="RECEIPT_INGEST_SKIPPED",
            reason=f"unregistered slack_user_id: {slack_user_id}",
        )
        try:
            post_message(
                channel=event["channel"],
                text="아직 연결되지 않은 계정이에요. PayPal 수취 이메일 주소를 이 채널에 답장해주시면 연결해드릴게요.",
            )
        except SlackSendError:
            pass
        return {"status": "ignored", "reason": "unregistered_user"}

    files = [f for f in event.get("files", []) if f.get("mimetype") in _IMAGE_MIMETYPES]
    if not files:
        return {"status": "ignored"}

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
                org_id=org_id,
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


@router.post("/slack/interactions")
async def slack_interactions(request: Request):
    """schema-contract.md §10 — 버튼 응답. Slack 앱 설정의 Interactivity Request URL.

    이게 없으면 `RESPONDED`가 영영 안 생겨 모든 claim_request가 만료된다.

    **본문이 form-encoded다**(`payload=<json>`). 서명은 여기서도 raw body 기준이라
    /slack/events와 같은 함수를 쓴다 — 파싱한 값을 다시 직렬화하면 서명이 깨진다.

    **Slack은 3초 안에 200을 원한다.** 하는 일이 Firestore 트랜잭션 하나뿐이라
    큐로 넘기지 않는다. 다만 실패해도 500을 내지 않는다 — Slack이 재전송하면
    같은 버튼 클릭이 여러 번 오고, 그때마다 사용자에게는 오류 배너가 뜬다.
    """
    raw_body = await request.body()
    try:
        verify_slack_signature(
            raw_body,
            request.headers.get("X-Slack-Request-Timestamp", ""),
            request.headers.get("X-Slack-Signature", ""),
        )
    except SignatureError as e:
        raise HTTPException(status_code=401, detail=str(e))

    # python-multipart(Form 의존성)를 끌어오지 않는다 — 필요한 건 키 하나다.
    fields = parse_qs(raw_body.decode("utf-8"))
    raw_payload = (fields.get("payload") or [""])[0]
    try:
        payload = json.loads(raw_payload)
    except ValueError:
        # 서명은 통과했으므로 Slack이 보낸 게 맞다. 모양을 모르면 무시한다 —
        # 400을 내면 Slack이 재전송하는데 다시 불러도 같은 본문이다.
        _audit_best_effort(
            actor="api/src/ingest",
            action="SLACK_INTERACTION_UNPARSABLE",
            reason="payload field is not valid JSON",
        )
        return {"status": "ignored", "reason": "unparsable"}

    if payload.get("type") != "block_actions":
        # view_submission·shortcut 등은 아직 쓰지 않는다.
        return {"status": "ignored", "reason": "unsupported_type"}

    actions = payload.get("actions") or []
    action = next(
        (a for a in actions if isinstance(a, dict) and a.get("action_id") == CLAIM_REQUEST_ACTION_ID),
        None,
    )
    if action is None:
        return {"status": "ignored", "reason": "unsupported_action"}

    claim_request_id = action.get("value")
    if not claim_request_id:
        _audit_best_effort(
            actor="api/src/ingest",
            action="SLACK_INTERACTION_NO_TARGET",
            reason=f"{CLAIM_REQUEST_ACTION_ID} without a claim_request_id value",
        )
        return {"status": "ignored", "reason": "no_target"}

    now = datetime.now(UTC)
    try:
        transitioned = mark_responded(claim_request_id, now=now)
    except ReceiptStoreUnavailable as e:
        # 트랜잭션 소진. 다시 부르면 될 실패지만 Slack 재전송에 기대지 않는다 —
        # 사람에게는 이미 오류로 보이고, 남은 건 만료뿐이라 조용히 사라지지 않는다.
        _audit_best_effort(
            actor="api/src/ingest",
            action="CLAIM_REQUEST_RESPONSE_FAILED",
            reason=str(e),
            after={"claim_request_id": claim_request_id},
        )
        return {"status": "ignored", "reason": "store_unavailable"}

    if not transitioned:
        # 이미 RESPONDED(재전송)이거나 EXPIRED다. 만료를 응답으로 되살리지 않는다.
        return {"status": "ignored", "reason": "not_open"}

    record_audit_log(
        actor="api/src/ingest",
        action="CLAIM_REQUEST_RESPONDED",
        reason=f"slack_user_id={(payload.get('user') or {}).get('id')}",
        after={"claim_request_id": claim_request_id, "status": "RESPONDED"},
    )
    return {"status": "ok", "claim_request_id": claim_request_id}


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
        result, claim_request_id = apply_claimant_verdict(
            receipt_id, verdict, now=datetime.now(UTC)
        )
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

    # 재요청이 필요하다고 판정됐으면 재촉 루프를 곧바로 깨운다(delay 0) — 최초 DM은
    # 이 첫 깨어남이 보낸다. 실패는 삼킨다: 전이는 이미 커밋됐고 여기서 500을 내면
    # 재시도가 CAS 가드에 걸려 SKIPPED로 빠져 재촉이 영영 안 붙는다.
    # parsing/pipeline.py의 CLAIMANT_ENQUEUE_FAILED와 같은 형태다.
    if result == "REQUERY" and claim_request_id:
        _try_enqueue_remind(claim_request_id, 0)

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


def _requery_message(receipt_id: str | None, recipient_id: str | None) -> str | None:
    """DM 본문은 청구자 에이전트가 쓴 `requery_message`에서만 온다. 없으면 None —
    **코드가 대체 문장을 만들지 않는다.** 문안을 여기서 지어내면 "에이전트가 뭐라고
    했는지"의 기록이 사라지고, 사람은 에이전트가 판단한 적 없는 이유로 재요청을 받는다.

    draft 읽기는 settlements/store.py의 get_agent_draft(agent_drafts의 유일한
    읽기)를 그대로 쓴다 — task_apply_claimant_draft와 같은 경로다.

    영어 문안(requery_message_en)은 agent_drafts.py가 Gemma로 번역해 채운다
    (payflow-agent는 한국어만 쓴다). 수취인 Slack 계정의 locale이 en으로
    시작하면 그걸 쓰고, 없거나 조회 실패하면 한국어로 폴백한다 — 번역이
    "안 됨"이 발송 자체를 막으면 안 된다."""
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

    recipient = get_recipient(recipient_id) if recipient_id else None
    slack_user_id = (recipient or {}).get("slack_user_id")
    locale = get_user_locale(slack_user_id) if slack_user_id else None
    if locale and locale.startswith("en"):
        message_en = payload.get("requery_message_en")
        if isinstance(message_en, str) and message_en.strip():
            return message_en
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


# 최초 발송과 재촉 사이의 기본 간격. **CLAIM_REQUEST_TTL_SECONDS에서 파생시킨다 —
# 고정 리터럴로 두면 안 된다.** 이 값이 TTL과 같으면(둘 다 86400이던 이전 상태)
# apply_claimant_verdict가 만든 `expires_at = t0 + TTL`과 최초 발송 뒤 깨어남
# 시각이 정확히 겹쳐서, 깨어난 태스크가 decide()에서 SEND_REMINDER가 아니라
# EXPIRE로 빠진다. 큐 전달 지연과 무관한 결정론적 결과다 — env를 아무것도 안 넣은
# 환경에서 **재촉 DM이 0회 나가고** CLAIM_REQUEST_EXPIRED만 정상처럼 남는다.
# TTL의 절반으로 두면 TTL을 env로 바꿔도 "재촉이 만료보다 먼저"라는 관계가 유지된다.
#
# 배포는 infra/cloud_run.tf가 이 값을 주입한다(var.reminder_delay_seconds,
# 현재 20 — 데모용). 즉 배포 환경에서 도는 값은 이 기본값이 아니다.
# docs/README.md — "환경변수 하나로 데모(20초)와 실제를 전환한다".
_DEFAULT_REMINDER_DELAY_SECONDS = str(CLAIM_REQUEST_TTL_SECONDS // 2)


def _expiry_delay_seconds(claim_request: dict, now: datetime) -> int | None:
    """`expires_at`까지 남은 초. 만료 시각을 모르면(파싱 실패·부재) None —
    decide()가 그 경우 SKIP을 내는 것과 같은 판단이다. 추측한 시각으로 예약하면
    아직 유효한 건이 일찍 만료된다.

    **올림이다.** int()는 0 방향 절삭이라 Firestore 타임스탬프(마이크로초가
    있다)에서는 다음 깨어남이 expires_at보다 최대 1초 이르게 잡힌다. 그 태스크가
    도착하면 아직 만료 전이라 decide()가 SKIP을 내고, SKIP 분기는 재예약하지
    않는다 — claim_request가 영구 정체하고 영수증도 NEEDS_REQUERY로 남는다.
    큐 전달이 보통 예정보다 늦다는 데 정확성을 의탁할 수 없다.
    """
    expires_at = parse_expires_at(claim_request.get("expires_at"))
    if expires_at is None:
        return None
    remaining = math.ceil((expires_at - now).total_seconds())
    return None if remaining < 0 else remaining


def _next_delay_seconds(action: ReminderAction, claim_request: dict, now: datetime) -> int | None:
    """다음 깨어남까지의 초. 예약하지 않아야 하면 None.

    최초 발송 뒤에는 REMINDER_DELAY_SECONDS 뒤에 재촉을 깨우고, 재촉을 보낸
    뒤에는 `expires_at`에 맞춰 만료 태스크를 깨운다.
    """
    if action == ReminderAction.SEND_INITIAL:
        return int(os.environ.get("REMINDER_DELAY_SECONDS", _DEFAULT_REMINDER_DELAY_SECONDS))
    return _expiry_delay_seconds(claim_request, now)


def _schedule_expiry(claim_request_id: str, claim_request: dict, now: datetime) -> None:
    """발송 없이 끝나는 경로의 생명선. `/tasks/remind`는 **자기 재예약이 유일한
    생명선이다** — Cloud Scheduler도 스윕 크론도 없다. 재예약을 안 붙이고 200으로
    끝내면 claim_request가 PENDING에 영구 고착하고(만료조차 안 된다) receipt는
    NEEDS_REQUERY, claim은 DRAFT로 남아 정산 run 선정(CONFIRMED만 진입)에서
    영구 제외된다 — 정당한 지출이 감사 로그 한 줄만 남기고 영영 지급되지 않는다.

    그래서 이 경로들도 `expires_at`에 만료 태스크를 건다. 재촉 DM을 다시 시도하는
    게 아니다(같은 실패가 반복될 뿐이다) — 건을 EXPIRED로 닫아 사람이 볼 수 있는
    종착에 도달시키는 것이다.
    """
    delay_seconds = _expiry_delay_seconds(claim_request, now)
    if delay_seconds is not None:
        _try_enqueue_remind(claim_request_id, delay_seconds)


def _try_enqueue_remind(claim_request_id: str, delay_seconds: int) -> None:
    """enqueue 실패는 삼킨다. 상태 전이도 Slack 발송도 이미 끝난 뒤라 여기서
    500을 내면 재시도가 CAS에 걸려 아무 일도 못 하고 같은 자리에서 다시 죽는다.
    parsing/pipeline.py의 CLAIMANT_ENQUEUE_FAILED와 같은 형태다.

    **여기서 삼킨 실패는 루프를 끊는다.** REMIND_ENQUEUE_FAILED가 남았다는 건
    다음 깨어남이 없다는 뜻이고(자기 재예약이 유일한 생명선이다), 그 건은
    PENDING/REMINDED에 고착한다. 다른 흡수 상태들과 달리 이건 같은 수단으로 못
    고친다 — 재예약 수단 자체가 실패한 것이다. 복구는 큐 밖에서 와야 한다:
    감사 로그의 REMIND_ENQUEUE_FAILED를 보고 수동 재개하거나, 스윕 크론
    (infra/ 소유)이 생기면 거기서 줍는다."""
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

        message = _requery_message(receipt_id, claim_request.get("recipient_id"))
        if message is None:
            # 문안이 없으면 보내지 않는다. 재시도해도 draft는 그대로다.
            # 만료 태스크를 **감사 로그보다 먼저** 건다 — 감사 싱크가 죽어서
            # 500으로 뒤집혀도 생명선은 이미 붙어 있어야 한다.
            _schedule_expiry(claim_request_id, claim_request, now)
            record_audit_log(
                actor="api/src/ingest",
                action="CLAIM_REQUEST_NO_MESSAGE",
                reason=f"agent_drafts/CLAIMANT:{receipt_id}에 requery_message가 없다",
                after={"claim_request_id": claim_request_id, "receipt_id": receipt_id},
            )
            return {"status": "ignored", "reason": "no_message"}

        target = _resolve_target(claim_request, receipt_id)
        if target is None:
            _schedule_expiry(claim_request_id, claim_request, now)
            record_audit_log(
                actor="api/src/ingest",
                action="CLAIM_REQUEST_NO_TARGET",
                reason=f"recipient {claim_request.get('recipient_id')}에 slack_user_id가 없다",
                after={"claim_request_id": claim_request_id, "receipt_id": receipt_id},
            )
            return {"status": "ignored", "reason": "no_target"}
        channel, thread_ts = target

        # **다음 지연은 발송 전에 계산한다.** REMINDER_DELAY_SECONDS가 비정수면
        # int()가 ValueError를 던지는데, 이 계산이 발송 뒤에 있으면 DM은 이미
        # 나간 뒤 500이 나고 재예약은 영영 안 붙는다 — 위 _schedule_expiry가
        # 없애려는 것과 같은 종류의 흡수 상태다. 발송 전이면 아무것도 하지 않은
        # 채 500이 되고 재시도가 처음부터 다시 돈다.
        delay_seconds = _next_delay_seconds(action, claim_request, now)

        # 발송 직전 CAS. 태스크가 중복 전달되면 같은 DM이 두 번 나간다.
        if not claim_send_slot(claim_request_id, expected_action=action, now=now):
            return {"status": "ignored", "reason": "slot_taken"}

        try:
            slack_ts = post_message(
                channel=channel,
                text=message,
                thread_ts=thread_ts,
                blocks=requery_blocks(message, claim_request_id),
            )
        except SlackSendTransient as e:
            # 아직 아무것도 기록하지 않았다 — 재시도가 처음부터 다시 돈다.
            raise HTTPException(status_code=503, detail=f"slack send failed: {e}")
        except SlackSendPermanent as e:
            # not_in_channel/channel_not_found. 다시 보내도 같은 실패라 재시도는
            # 안 하지만, 건은 닫아야 한다 — 봇이 채널에 초대돼 있지 않으면
            # _resolve_target이 고르는 스레드 경로 전체가 여기로 빠진다
            # (DM 폴백은 계획서가 명시적으로 미결로 올린 범위 밖이다).
            _schedule_expiry(claim_request_id, claim_request, now)
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
        # 재시도해도 안전하다. 감사 로그는 best-effort다 — 여기서 던지면 503이
        # 500으로 뒤집혀 큐가 보는 재시도 신호가 바뀐다.
        _audit_best_effort(
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

    if delay_seconds is not None:
        _try_enqueue_remind(claim_request_id, delay_seconds)

    return {"status": "ok", "action": action.value, "slack_ts": slack_ts}


def _format_amount(amount_minor: int, currency: str) -> str:
    """사람이 읽을 금액 표시. payouts/currency.py의 CURRENCY_EXPONENT를 그대로
    쓴다 — 등록 안 된 통화는 exponent 0으로 본다(여기 오는 값은 이미 실제로
    지급까지 끝난 sender_item 금액이라 등록 안 된 통화일 일은 없다)."""
    exponent = CURRENCY_EXPONENT.get(currency, 0)
    if exponent == 0:
        return f"{amount_minor:,} {currency}"
    whole, remainder = divmod(amount_minor, 10**exponent)
    return f"{whole:,}.{remainder:0{exponent}d} {currency}"


def _settlement_complete_text(
    *, amount_display: str, rejected_items: list[dict], locale: str | None
) -> str:
    """정산 완료 DM 본문 — "정산이 완료됐다"가 메인이고, 이번 run에서 제외된
    물품이 있으면 그 아래에 물품명·사유를 덧붙인다.

    reason은 집행자 에이전트(executor/tools.py flag_personal_use_items)가 쓴
    한국어 문장이면 그대로 옮긴다 — _requery_message와 같은 원칙으로 여기서
    대체 문장을 지어내지 않는다. 사람이 web에서 사유 없이 직접 체크를 해제한
    항목은 reason이 없다 — 그땐 "집행자가 직접 반려했다"는 사실 자체를
    알리는 문구로 대체한다(없는 사유를 지어내진 않되, 누가 뺐는지는 사실이므로
    숨기지 않는다).

    영어 로케일이면 이 시점(Cloud Tasks가 부르는 태스크)에 Gemma로 번역한다 —
    reject-items 요청(agent → api, 10초 타임아웃) 안에서 번역하면 그 예산을
    갉아먹어 반려 자체가 실패할 위험이 있어 여기로 미뤘다. 번역 실패는
    한국어 사유로 폴백한다(헤더만 영어) — 번역이 안 됐다고 사유 자체를 숨기지
    않는다.
    """
    fallback_reason_ko = "이 항목이 집행자에 의해 반려되었습니다"
    lines_ko = [
        f"- {i.get('name') or '(이름 없음)'}: {i.get('reason') or fallback_reason_ko}"
        for i in rejected_items
    ]

    if locale and locale.startswith("en"):
        header = f"Your settlement of {amount_display} has been completed."
        if not rejected_items:
            return header
        translated = translate_lines(
            [f"{i.get('name') or ''}: {i.get('reason') or fallback_reason_ko}" for i in rejected_items]
        )
        body = "\n".join(f"- {t}" for t in translated) if translated is not None else "\n".join(lines_ko)
        return f"{header}\n\nThe following items were excluded from this settlement:\n{body}"

    header = f"{amount_display} 정산이 완료되었습니다."
    if not rejected_items:
        return header
    return f"{header}\n\n다음 항목은 이번 정산에서 제외됐습니다:\n" + "\n".join(lines_ko)


@router.post("/tasks/notify-settlement-complete")
def task_notify_settlement_complete(body: dict, authorization: str = Header(default="")):
    """정산 완료 통보 — payouts/reconcile.py.reconcile()이 claim을 SETTLED로
    바꾼 직후(=PayPal 송금이 실제로 성공한 직후) enqueue한다.

    **정산 완료 시점을 고른 이유**: 물품 반려는 승인 전까지 web에서 언제든
    되돌릴 수 있는 잠정 상태다(settlements/routes.py._apply_item_exclusion).
    승인 시점에 알리면 아직 돈이 안 나간 상태를 "정산 완료"라고 잘못 말하게
    되고, 반려 직후에 바로 알리면 사람이 나중에 되돌린 반려까지 통보하게
    된다 — 송금까지 끝난 이 시점이라야 되돌릴 수 없는 확정된 사실만 안전하게
    전달할 수 있다.

    반려 판단 기준은 excluded=true 전체다 — 집행자 에이전트가 자동으로 뗀
    것(rejected_by="EXECUTOR")과 사람이 web 체크박스로 직접 뗀 것을 구분하지
    않는다. 청구자 입장에서는 "누가 뺐는지"가 아니라 "무엇이 왜 빠졌는지"가
    중요하다.

    recipients는 reconcile()이 넘긴다 — 이번 호출에서 SUCCESS로 확정된
    sender_item(recipient_id, amount_minor, currency)만큼만. 부분 실패 run에서
    아직 결과가 안 난 recipient에게는 여기서 알리지 않는다(재발송이 나중에
    성공하면 그때 별도로 통보된다).
    """
    verify_oidc(authorization)

    run_id = body.get("settlement_run_id")
    recipients = body.get("recipients")
    if not run_id or not recipients:
        raise HTTPException(status_code=400, detail="settlement_run_id, recipients required")

    claims = get_claims_for_run(run_id)
    receipts = get_receipts({c["receipt_id"] for c in claims})

    rejected_by_recipient: dict[str, list[dict]] = {}
    for c in claims:
        receipt = receipts.get(c["receipt_id"]) or {}
        for item in receipt.get("items") or []:
            if not item.get("excluded"):
                continue
            rejected_by_recipient.setdefault(c["recipient_id"], []).append(
                {"name": item.get("name"), "reason": item.get("rejected_reason")}
            )
        # claim 전체 반려(중복 청구·동일 영수증 재제출·미래 거래일 등,
        # settlements/routes.py._apply_claim_exclusion) — 물품 하나가 아니라
        # 영수증 전체가 빠진 경우라 가맹점명으로 어떤 청구인지 알려준다.
        if c.get("excluded"):
            merchant = receipt.get("merchant_name") or "가맹점 미상"
            rejected_by_recipient.setdefault(c["recipient_id"], []).append(
                {"name": f"{merchant} 청구 전체", "reason": c.get("rejected_reason")}
            )

    notified = 0
    had_transient_failure = False
    for r in recipients:
        recipient_id = r.get("recipient_id")
        recipient = get_recipient(recipient_id)
        slack_user_id = (recipient or {}).get("slack_user_id")
        if not slack_user_id:
            _audit_best_effort(
                actor="api/src/ingest",
                action="SETTLEMENT_COMPLETE_NOTICE_NO_TARGET",
                reason=f"recipient {recipient_id}에 slack_user_id가 없다",
                after={"settlement_run_id": run_id, "recipient_id": recipient_id},
            )
            continue

        locale = get_user_locale(slack_user_id)
        amount_display = _format_amount(r.get("amount_minor"), r.get("currency"))
        rejected_items = rejected_by_recipient.get(recipient_id, [])
        text = _settlement_complete_text(
            amount_display=amount_display, rejected_items=rejected_items, locale=locale
        )

        try:
            post_message(channel=slack_user_id, text=text)
        except SlackSendTransient as e:
            # 다른 수취인 발송은 계속한다 — 이 사람만 다시 큐가 재시도한다.
            had_transient_failure = True
            _audit_best_effort(
                actor="api/src/ingest",
                action="SETTLEMENT_COMPLETE_NOTICE_SEND_TRANSIENT_FAILURE",
                reason=str(e),
                after={"settlement_run_id": run_id, "recipient_id": recipient_id},
            )
            continue
        except SlackSendPermanent as e:
            # 다시 보내도 같은 실패다 — 이 사람은 포기하고 나머지는 계속한다.
            _audit_best_effort(
                actor="api/src/ingest",
                action="SETTLEMENT_COMPLETE_NOTICE_SEND_FAILED",
                reason=str(e),
                after={"settlement_run_id": run_id, "recipient_id": recipient_id},
            )
            continue

        notified += 1
        _audit_best_effort(
            actor="api/src/ingest",
            action="SETTLEMENT_COMPLETE_NOTICE_SENT",
            after={
                "settlement_run_id": run_id,
                "recipient_id": recipient_id,
                "rejected_item_count": len(rejected_items),
            },
        )

    if had_transient_failure:
        # 큐가 이 태스크 전체를 재시도한다 — 이미 성공한 수취인에게 같은 DM이
        # 한 번 더 갈 수 있다. 금액이 걸린 동작이 아니라(money-safety.md 대상
        # 아님) claim_send_slot 같은 CAS까지는 두지 않는다.
        raise HTTPException(status_code=503, detail="one or more slack sends failed transiently")

    return {"status": "ok", "notified": notified}
