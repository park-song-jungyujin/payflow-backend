"""schema-contract.md §2 — 파싱이 쓰는 Firestore 창구 (A 소유).

ingest/store.py와 같은 컬렉션(`receipts`)을 만지지만 시점이 다르다 — 인입은
문서를 만들고(RECEIVED), 파싱은 그 문서를 갱신한다(PARSED/FAILED). 여기엔
트랜잭션이 없다: dedup은 이미 인입에서 끝났고, 파싱 갱신은 단일 문서 쓰기라
CAS가 필요 없다. 재시도 멱등성은 pipeline이 status로 거른다.

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


def commit_parsed_with_claim(receipt_id: str, updates: dict, claim: dict | None) -> None:
    """receipts의 PARSED 갱신과 claims 생성을 **한 트랜잭션**으로 쓴다.

    갈라놓으면 갱신은 됐는데 claim 생성이 실패한 경우가 복구되지 않는다 —
    재시도가 와도 status != RECEIVED라 파이프라인이 SKIPPED로 빠지고, 그
    영수증은 영원히 청구 없이 남는다.

    claim이 None이면 receipts만 갱신한다(금액을 못 읽어 청구를 못 만드는 경우).
    """
    client = get_client()

    @firestore.transactional
    def _txn(transaction):
        transaction.update(client.collection("receipts").document(receipt_id), updates)
        if claim is not None:
            transaction.set(client.collection("claims").document(claim["claim_id"]), claim)

    _txn(client.transaction())
