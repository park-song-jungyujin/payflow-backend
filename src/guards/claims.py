"""schema-contract.md §2 `claims` — "claim 점유는 CAS 트랜잭션이다. 한 claim이
두 배치에 들어가면 이중 지급이다." · "이 전이는 `api/src/guards/`(C)가 담당한다."

`payouts/store.py.link_claims_to_run`은 조건 없이 덮어쓰는 batch write라 이 보장이
없다(TEMP) — 이 모듈이 실제 CAS를 한다. `settlements/routes.py`(B)가 정산 실행
생성 시 이 함수를 부른다.
"""

from datetime import UTC, datetime

from google.cloud import firestore

from ..payouts.store import get_client


def link_claims_to_run_cas(run_id: str, claim_ids: list[str]) -> list[str]:
    """하나의 Firestore 트랜잭션 안에서 모든 claim_id의 현재 status를 재확인하고,
    `CONFIRMED`인 것만 `IN_RUN`으로 전이하며 `settlement_run_id`를 기록한다. 이미
    다른 배치가 먼저 채간 claim(더는 CONFIRMED가 아님)은 조용히 빠진다 — 배치
    전체를 실패시키지 않는다(schema-contract.md §2 "이미 IN_RUN인 claim은 전이
    실패로 배치에서 빠진다"). 반환값은 실제로 링크에 성공한 claim_id만이라
    호출부가 이 목록으로 나머지(요약·중복 판정·엔큐)를 다시 좁혀야 한다."""
    client = get_client()
    col = client.collection("claims")

    @firestore.transactional
    def _txn(transaction):
        refs = [col.document(cid) for cid in claim_ids]
        snapshots = [ref.get(transaction=transaction) for ref in refs]
        now = datetime.now(UTC)
        linked = []
        for ref, snapshot in zip(refs, snapshots):
            data = snapshot.to_dict() if snapshot.exists else None
            if data is None or data.get("status") != "CONFIRMED":
                continue
            transaction.update(
                ref, {"settlement_run_id": run_id, "status": "IN_RUN", "updated_at": now}
            )
            linked.append(data["claim_id"])
        return linked

    return _txn(client.transaction())
