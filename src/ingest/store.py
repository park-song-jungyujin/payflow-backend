"""schema-contract.md §2 — Slack 인입이 쓰는 Firestore 창구(A 소유).

payouts/store.py(C 소유)와 컬렉션이 겹치지 않는다. `get_client`만 재사용한다 —
Firestore 클라이언트를 두 번 만들 이유가 없고, C 파일은 읽기만 한다.

**dedup이 이 모듈의 핵심이다.** Slack은 ack가 3초를 넘기면 같은 이벤트를 최대 3회
재전송하고 Cloud Tasks도 재시도한다. receipts 문서가 두 개 생기면 파싱이 두 번 돌고
claims가 두 건 생겨 이중 지급으로 이어진다.

**유일성은 문서 ID로 강제한다.** Firestore 트랜잭션은 읽어서 *반환된* 문서에만 락을
건다 — `slack_file_id == X` 쿼리가 0건이면 잠글 대상이 없어서 동시 요청 두 개가 각각
"없음"을 보고 서로 다른 `rct_{ulid}`에 쓰고, 문서 ID가 달라 충돌 없이 둘 다 커밋된다.
쿼리 기반 dedup은 동시 재전송에서 뚫린다. 결정론적 경로(`receipt_dedup_keys/{file_id}`)에
대한 `transaction.get()`은 문서가 없어도 read set에 들어가 락이 걸린다.
"""

import logging
import os
from datetime import UTC, datetime, timedelta

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from ulid import ULID

from ..guards.audit import record_audit_log
from ..ingest.drafts import DraftVerdict
from ..payouts.store import get_client

DEDUP_COLLECTION = "receipt_dedup_keys"

_ACTOR = "api/src/ingest"

# schema-contract.md에도, .env·infra 어디에도 없다 — 이 변수는 의도적으로 미결이다.
# settlements/store.py의 같은 이름 변수(기본값 604800초, 검증 실패 재촉용)와는
# 별개 용도라 기본값이 다르다. 나중에 정리가 필요하면 두 값을 합칠지 논의한다.
CLAIM_REQUEST_TTL_SECONDS = int(os.environ.get("CLAIM_REQUEST_TTL_SECONDS", "86400"))

# 강등하면 안 되는 claim 상태 — 이미 배치에 점유됐거나(IN_RUN) 송금이 끝났다(SETTLED).
# 강등하면 "어느 run에도 안 속하면서 settlement_run_id를 들고 있는" 계약 밖 상태가
# 생긴다. claim은 그대로 두고 감사 로그로 불일치만 남긴다.
_CLAIM_STATUSES_NOT_DEMOTABLE = {"IN_RUN", "SETTLED"}


def find_or_create_recipient(org_id: str, slack_user_id: str) -> dict:
    """schema-contract.md "org 스코핑과 로그인" — 초대 절차 없이 최초 메시지
    시점에 `recipients`를 만든다. 이미 있으면 그대로 돌려준다.

    Slack 프로필(`display_name`·이메일)을 조회하지 않는다 — 첫 영수증 처리에
    필요하지 않고, PayPal 지급에 필요한 `paypal_email`은 어차피 별도 등록
    절차가 있어야 한다(범위 밖). 최소 필드로 자리만 만든다."""
    existing = find_recipient_by_slack_user(org_id, slack_user_id)
    if existing is not None:
        return existing

    recipient_id = f"rcp_{ULID()}"
    now = datetime.now(UTC)
    doc = {
        "recipient_id": recipient_id,
        "org_id": org_id,
        "slack_user_id": slack_user_id,
        "paypal_email": "",
        "display_name": slack_user_id,
        "monthly_paid_minor": 0,
        "monthly_period": now.strftime("%Y-%m"),
        "verified": False,
        "status": "ACTIVE",
        "created_at": now,
        "updated_at": now,
    }
    get_client().collection("recipients").document(recipient_id).set(doc)
    return doc


class ReceiptStoreUnavailable(RuntimeError):
    """영수증을 저장하지 못했다. 호출부가 503으로 바꾼다.

    SDK는 트랜잭션 재시도를 소진하면 `ValueError`를 던지는데, 라우트가 그걸 직접
    잡으면 무관한 `ValueError`까지 같이 삼킨다. 경계에서 도메인 예외로 바꿔 올린다.
    """


def _run_in_transaction(fn):
    """트랜잭션 실행 경계. 테스트가 이 함수만 갈아끼워 실제 Firestore 없이
    콜백 본문을 돌린다.

    google-cloud-firestore에 `Client.run_transaction`은 없다. 파이썬 SDK의
    관용 형태는 `@firestore.transactional` + `client.transaction()`이고,
    guards/tokens.py·guards/routes.py도 이 형태를 쓴다. 데코레이터는 콜백의
    반환값을 그대로 돌려주므로 (receipt_id, created) 튜플이 밖으로 나온다.
    ABORTED면 SDK가 콜백을 통째로 재실행한다 — 조회부터 다시 도므로 안전하다.
    """
    client = get_client()
    return firestore.transactional(fn)(client.transaction())


def find_recipient_by_slack_user(org_id: str, slack_user_id: str) -> dict | None:
    """schema-contract.md §2 — recipients의 Slack ID 매핑 조회는 A 소유다.
    `slack_user_id`는 워크스페이스(기관) 안에서만 유일하므로 `org_id`와
    함께 걸러야 다른 기관의 동명 ID와 안 겹친다. 두 동등 필터라 복합 색인이
    필요하다(Firestore가 첫 호출에 콘솔 링크를 준다)."""
    docs = (
        get_client()
        .collection("recipients")
        .where(filter=FieldFilter("org_id", "==", org_id))
        .where(filter=FieldFilter("slack_user_id", "==", slack_user_id))
        .limit(1)
        .stream()
    )
    doc = next(iter(docs), None)
    return doc.to_dict() if doc else None


def create_receipt_if_absent(
    *,
    org_id: str,
    recipient_id: str,
    slack_file_id: str,
    slack_channel_id: str,
    slack_message_ts: str,
) -> tuple[str, bool]:
    """(receipt_id, created)를 돌려준다. created=False면 Slack 재전송이다.

    `receipts`의 문서 ID는 §3대로 `receipt_id`(`rct_{ulid}`)를 유지한다 —
    settlements/export.py가 `.document(receipt_id).get()`으로 읽고 seed_firestore.py·
    fixture도 같은 규칙이다. dedup은 별도 컬렉션의 문서 ID(`slack_file_id`)가 맡는다.

    dedup 문서에 `receipt_id`를 같이 담으므로 재전송 경로는 쿼리 없이 문서 직접
    조회로 끝난다 — `receipts.slack_file_id` 인덱스에 의존하지 않는다.
    """

    def _txn(transaction):
        dedup_ref = get_client().collection(DEDUP_COLLECTION).document(slack_file_id)

        # Firestore 트랜잭션은 모든 읽기가 모든 쓰기보다 앞에 와야 한다. 그리고
        # 이 읽기는 문서가 없어도 read set에 들어가 락을 건다 — 동시 재전송 둘 중
        # 하나는 커밋에서 ABORTED를 받고, 재실행 때 상대가 쓴 문서를 보게 된다.
        snapshot = dedup_ref.get(transaction=transaction)
        if snapshot.exists:
            return snapshot.get("receipt_id"), False

        receipt_id = f"rct_{ULID()}"
        now = datetime.now(UTC)

        transaction.set(
            dedup_ref,
            {
                "slack_file_id": slack_file_id,
                "receipt_id": receipt_id,
                "created_at": now,
            },
        )
        # 파싱 파이프라인 몫(image_gcs_uri·currency·parse_signals 등)은 쓰지 않는다.
        # 계약상 전부 nullable이고, 여기서 자리만 만들어두면 "채워졌는지"를
        # 구분할 수 없어진다.
        transaction.set(
            get_client().collection("receipts").document(receipt_id),
            {
                "receipt_id": receipt_id,
                "org_id": org_id,
                "recipient_id": recipient_id,
                "slack_file_id": slack_file_id,
                "slack_channel_id": slack_channel_id,
                "slack_message_ts": slack_message_ts,
                "status": "RECEIVED",
                "created_at": now,
                "updated_at": now,
            },
        )
        return receipt_id, True

    try:
        return _run_in_transaction(_txn)
    except ValueError as e:
        # SDK는 재시도 소진 시 ValueError("Failed to commit transaction in N attempts.")
        # 를 던진다. 경계에서 도메인 예외로 바꿔 라우트가 SDK 사정을 모르게 한다.
        raise ReceiptStoreUnavailable(str(e)) from e


def apply_claimant_verdict(receipt_id: str, verdict: DraftVerdict, *, now: datetime) -> str:
    """청구자 에이전트의 draft 판정을 receipts.status·claims.status·claim_requests
    생성에 **한 트랜잭션**으로 반영한다. `"REQUERY"` / `"APPLIED"` / `"SKIPPED"`를 돌려준다.

    갈라놓으면 "영수증은 NEEDS_REQUERY인데 claim은 CONFIRMED"인 창이 생긴다 — 그
    사이 정산 배치가 돌면 분쟁 중인 영수증이 송금된다. 재시도로도 안 메워진다:
    두 번째 시도는 이미 NEEDS_REQUERY라 아래 CAS 가드에 걸려 SKIPPED로 빠진다.
    `create_receipt_if_absent`(이 파일)·`commit_parsed_with_claim`(parsing/store.py)와
    같은 패턴이다 — 모든 읽기가 모든 쓰기보다 앞에 온다.
    """
    client = get_client()
    receipt_ref = client.collection("receipts").document(receipt_id)

    def _txn(transaction):
        # receipts를 read set에 넣어 락을 건다. 동시 시도 중 하나는 ABORTED로
        # 재실행되고, 재실행 시점에는 status != PARSED를 보고 멈춘다 — 이게
        # "같은 draft 재실행"·"서로 다른 draft 두 개"에서 claim_requests가
        # 한 건만 생기는 이유다.
        snapshot = receipt_ref.get(transaction=transaction)
        # to_dict()가 없으면(문서 없음) 빈 dict로 둔다 — snapshot.get(field)는
        # 필드가 없으면 KeyError를 던지는데, 그러면 이 트랜잭션이 그대로 500으로
        # 터진다. tokens.py의 verify_and_burn_token과 같은 관용구.
        data = snapshot.to_dict() or {}
        if not snapshot.exists or data.get("status") != "PARSED":
            return "SKIPPED", None

        recipient_id = data.get("recipient_id")
        org_id = data.get("org_id")

        # claim 조회. 파싱은 영수증당 claim을 1회만 만들고(claims.py 주석) 동시
        # 생성 경로가 없다는 게 전제다 — 그래서 쿼리 결과가 0건이어도(락이
        # 안 걸려도) 안전하다. 청구 분할처럼 한 영수증에서 claim이 여럿 생기는
        # 기능이 붙으면 이 전제가 깨지므로 그때 다시 봐야 한다.
        claim_docs = list(
            client.collection("claims")
            .where(filter=FieldFilter("receipt_id", "==", receipt_id))
            .limit(1)
            .stream(transaction=transaction)
        )
        claim_snapshot = claim_docs[0] if claim_docs else None
        # 같은 이유로 claims 문서도 to_dict() 경유 — status 필드가 없어도 KeyError
        # 대신 None이 되게 한다.
        claim_data = (claim_snapshot.to_dict() or {}) if claim_snapshot is not None else None

        # 필드 누락 방어. recipient_id·org_id 없는 receipts나 status 없는 claims는
        # 계약 밖 문서다 — 그냥 흘려보내면 recipient_id=None이거나 org_id=None인
        # claim_request가 나가거나(§3 ClaimRequest는 둘 다 non-nullable) 상태를
        # 알 수 없는 claim을 잘못 판단하게 된다. 쓰기 없이 SKIPPED로 멈춘다.
        if (
            recipient_id is None
            or org_id is None
            or (claim_snapshot is not None and claim_data.get("status") is None)
        ):
            return "SKIPPED", None

        # --- 여기서부터 쓰기. 위 읽기가 전부 끝난 뒤여야 한다. ---

        if verdict.needs_requery:
            transaction.update(receipt_ref, {"status": "NEEDS_REQUERY", "updated_at": now})

            demotion_blocked_status = None
            if claim_snapshot is not None:
                claim_status = claim_data.get("status")
                if claim_status == "CONFIRMED":
                    claim_ref = client.collection("claims").document(claim_snapshot.id)
                    transaction.update(claim_ref, {"status": "DRAFT", "updated_at": now})
                elif claim_status in _CLAIM_STATUSES_NOT_DEMOTABLE:
                    demotion_blocked_status = claim_status

            claim_request_id = f"crq_{ULID()}"
            transaction.set(
                client.collection("claim_requests").document(claim_request_id),
                {
                    "claim_request_id": claim_request_id,
                    "org_id": org_id,
                    "recipient_id": recipient_id,
                    "receipt_id": receipt_id,
                    "reason": "AMOUNT_MISMATCH",
                    "slack_dm_ts": None,
                    "reminded_at": None,
                    "expires_at": now + timedelta(seconds=CLAIM_REQUEST_TTL_SECONDS),
                    "status": "PENDING",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            return "REQUERY", demotion_blocked_status

        # DRAFT·CONFIRMED까지 덮어쓴다 — is_business는 approval_amount_hash에
        # 안 들어가고 송금 금액에 영향이 없다(claims.py DEFAULT_IS_BUSINESS 참고).
        # IN_RUN·SETTLED는 제외: 이미 배치에 점유됐거나 송금이 끝났다.
        if verdict.is_business is not None and claim_snapshot is not None:
            claim_status = claim_data.get("status")
            if claim_status in ("DRAFT", "CONFIRMED"):
                claim_ref = client.collection("claims").document(claim_snapshot.id)
                transaction.update(claim_ref, {"is_business": verdict.is_business, "updated_at": now})
        return "APPLIED", None

    try:
        result, demotion_blocked_status = _run_in_transaction(_txn)
    except ValueError as e:
        # create_receipt_if_absent와 같은 경계 — SDK가 트랜잭션 재시도를 소진하면
        # ValueError를 던진다. 호출부(Task 3)가 SDK 사정을 모르게 도메인 예외로 바꾼다.
        raise ReceiptStoreUnavailable(str(e)) from e

    # 감사 로그는 트랜잭션 밖에서 남긴다 — commit_parsed_with_claim(parsing/store.py)·
    # ingest/routes.py와 같은 판단이다. 이미 커밋된 상태 전이를 감사 로그 실패
    # 때문에 되돌릴 이유가 없고, Firestore add()는 어차피 트랜잭션 API가 아니다.
    #
    # SETTLED는 이미 돈이 나간 건이다 — 이 감사 로그가 "에이전트가 재요청이
    # 필요하다고 판단했는데 이미 송금됨"의 유일한 기록이라 흔적 없이 삼키면
    # 안 된다. parsing/pipeline.py의 CLAIM_CREATED/CLAIM_CREATED_AUDIT_FAILED
    # 쌍과 같은 형태로, 실패하면 별도 액션으로 한 번 더 남긴다.
    if demotion_blocked_status is not None:
        try:
            record_audit_log(
                actor=_ACTOR,
                action="CLAIM_DEMOTION_BLOCKED",
                reason=f"claim status={demotion_blocked_status}, receipt {receipt_id}이 NEEDS_REQUERY로 내려갔지만 claim은 이미 {demotion_blocked_status}라 유지됨",
                after={"receipt_id": receipt_id, "claim_status": demotion_blocked_status},
            )
        except Exception as e:
            try:
                record_audit_log(
                    actor=_ACTOR,
                    action="CLAIM_DEMOTION_BLOCKED_AUDIT_FAILED",
                    reason=str(e),
                    after={"receipt_id": receipt_id, "claim_status": demotion_blocked_status},
                )
            except Exception:
                logging.getLogger(__name__).error(
                    "CLAIM_DEMOTION_BLOCKED audit log failed twice for receipt %s (claim status=%s): %s",
                    receipt_id,
                    demotion_blocked_status,
                    e,
                )

    return result
