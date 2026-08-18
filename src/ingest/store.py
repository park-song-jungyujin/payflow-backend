"""schema-contract.md §2 — Slack 인입이 쓰는 Firestore 창구(A 소유).

payouts/store.py(C 소유)와 컬렉션이 겹치지 않는다. `get_client`만 재사용한다 —
Firestore 클라이언트를 두 번 만들 이유가 없고, C 파일은 읽기만 한다.

**dedup이 이 모듈의 핵심이다.** Slack은 ack가 3초를 넘기면 같은 이벤트를 최대 3회
재전송하고 Cloud Tasks도 재시도한다. receipts 문서가 두 개 생기면 파싱이 두 번 돌고
claims가 두 건 생겨 이중 지급으로 이어진다. 그래서 계약(§2)이 "slack_file_id 중복
검사와 receipts 생성은 하나의 Firestore 트랜잭션 안에서 한다"를 못박고 있다.
"""

from datetime import UTC, datetime

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from ulid import ULID

from ..payouts.store import get_client


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


def find_recipient_by_slack_user(slack_user_id: str) -> dict | None:
    """schema-contract.md §2 — recipients의 Slack ID 매핑 조회는 A 소유다.
    단일 동등 필터라 복합 색인이 필요 없다."""
    docs = (
        get_client()
        .collection("recipients")
        .where(filter=FieldFilter("slack_user_id", "==", slack_user_id))
        .limit(1)
        .stream()
    )
    doc = next(iter(docs), None)
    return doc.to_dict() if doc else None


def create_receipt_if_absent(
    *,
    recipient_id: str,
    slack_file_id: str,
    slack_channel_id: str,
    slack_message_ts: str,
) -> tuple[str, bool]:
    """(receipt_id, created)를 돌려준다. created=False면 Slack 재전송이다.

    문서 ID는 §3대로 receipt_id(`rct_{ulid}`)를 쓴다 — fixture·seed_firestore.py와
    같은 규칙이라 파싱 태스크가 조회로 우회할 필요가 없다. dedup은 문서 ID가 아니라
    slack_file_id 조회로 하되, 조회와 생성을 한 트랜잭션에 넣어 원자성을 확보한다.
    """

    def _txn(transaction):
        # Firestore 트랜잭션은 모든 읽기가 모든 쓰기보다 앞에 와야 한다.
        # 조회를 먼저 끝내고 아래에서만 쓴다.
        existing = (
            get_client()
            .collection("receipts")
            .where(filter=FieldFilter("slack_file_id", "==", slack_file_id))
            .limit(1)
            .stream(transaction=transaction)
        )
        found = next(iter(existing), None)
        if found is not None:
            return found.to_dict()["receipt_id"], False

        receipt_id = f"rct_{ULID()}"
        now = datetime.now(UTC)
        # 파싱 파이프라인 몫(image_gcs_uri·currency·parse_signals 등)은 쓰지 않는다.
        # 계약상 전부 nullable이고, 여기서 자리만 만들어두면 "채워졌는지"를
        # 구분할 수 없어진다.
        transaction.set(
            get_client().collection("receipts").document(receipt_id),
            {
                "receipt_id": receipt_id,
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

    return _run_in_transaction(_txn)
