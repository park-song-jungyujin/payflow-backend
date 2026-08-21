"""schema-contract.md §2 — 파싱이 쓰는 Firestore 창구 (A 소유).

ingest/store.py와 같은 컬렉션(`receipts`)을 만지지만 시점이 다르다 — 인입은
문서를 만들고(RECEIVED), 파싱은 그 문서를 갱신한다(PARSED/FAILED).

`commit_parsed_with_claim`은 트랜잭션 안에서 receipt를 먼저 읽어 status를
재확인한다(CAS) — Cloud Tasks가 같은 receipt_id를 동시에 두 번 전달하면,
트랜잭션 밖의 읽기만으로는 두 시도 모두 `status == RECEIVED`를 보고 각각
다른 `claim_id`로 claim을 만들어 이중 지급으로 이어진다. 트랜잭션 안의
`get`은 그 문서를 read set에 넣어 락을 걸므로, 동시 시도 중 하나는
ABORTED로 재실행되고 재실행 시점에는 status != RECEIVED를 보고 멈춘다.
`ingest/store.py`의 `create_receipt_if_absent`가 같은 패턴이다(읽기가 모든
쓰기보다 앞).

`get_client`만 payouts/store.py(C 소유)에서 재사용한다. ingest/store.py와 같은 선례다.
"""

from google.cloud import firestore

from ..payouts.store import get_client


class ReceiptNotFound(RuntimeError):
    pass


def get_receipt(receipt_id: str) -> dict | None:
    doc = get_client().collection("receipts").document(receipt_id).get()
    return doc.to_dict() if doc.exists else None


def update_receipt(receipt_id: str, updates: dict) -> None:
    get_client().collection("receipts").document(receipt_id).update(updates)


def commit_parsed_with_claim(receipt_id: str, updates: dict, claim: dict | None) -> bool:
    """receipts의 PARSED 갱신과 claims 생성을 **한 트랜잭션**으로 쓴다.

    갈라놓으면 갱신은 됐는데 claim 생성이 실패한 경우가 복구되지 않는다 —
    재시도가 와도 status != RECEIVED라 파이프라인이 SKIPPED로 빠지고, 그
    영수증은 영원히 청구 없이 남는다.

    claim이 None이면 receipts만 갱신한다(금액을 못 읽어 청구를 못 만드는 경우).

    실제로 확정했는지를 bool로 돌려준다. 트랜잭션 밖(pipeline.parse_receipt
    진입부)의 status 확인은 동시 전달을 막지 못한다 — Cloud Tasks가 같은
    receipt_id를 동시에 두 번 전달하면 둘 다 RECEIVED를 읽고 지나올 수 있다.
    그래서 여기서 트랜잭션 안에 다시 읽어 CAS로 확인한다: 이미 다른 시도가
    확정했으면(status != RECEIVED, 또는 문서 없음) 아무것도 쓰지 않고
    False를 돌려준다. 호출부는 False면 "SKIPPED"로 취급해 감사 로그·enqueue를
    중복 실행하지 않아야 한다.
    """
    client = get_client()
    receipt_ref = client.collection("receipts").document(receipt_id)
    claims_ref = client.collection("claims").document(claim["claim_id"]) if claim is not None else None

    @firestore.transactional
    def _txn(transaction):
        # Firestore 트랜잭션은 모든 읽기가 모든 쓰기보다 앞에 와야 한다. 이
        # 읽기가 receipt_ref를 read set에 넣어 락을 건다 — 동시 시도 중 하나는
        # 커밋에서 ABORTED를 받고 재실행되며, 재실행 때는 status != RECEIVED를
        # 보고 중단한다.
        snapshot = receipt_ref.get(transaction=transaction)
        if not snapshot.exists or snapshot.get("status") != "RECEIVED":
            return False
        transaction.update(receipt_ref, updates)
        if claim is not None:
            transaction.set(claims_ref, claim)
        return True

    return _txn(client.transaction())
