"""schema-contract.md §6 결정론적 매칭 — SettlementFilter 조건 적용.

이건 TEMP가 아니라 실제 후보 선정 로직이다. 중복 청구 판정(금액·날짜윈도우·
가맹점명)은 `duplicates.find_duplicate_groups`에 별도로 있다 — 후보를 배치에서
빼지 않는 advisory 판정이라 이 필터링과 섞지 않는다.

TODO(B): 검증(schema-contract.md §2 "검증") 반영은 아직 없다. `settlements/routes.py`가
이 함수의 후보 목록을 받아 receipt별로 검증을 돌리고 탈락분을 제외한 뒤에만
claims CONFIRMED → IN_RUN CAS로 넘긴다.
"""

from datetime import date

from ..payouts.store import get_client, list_confirmed_claims
from ..schemas.models import SettlementFilter


def select_claims_for_run(org_id: str, filter: SettlementFilter) -> list[dict]:
    candidates = list_confirmed_claims(org_id)

    if filter.recipient_ids is not None:
        candidates = [c for c in candidates if c["recipient_id"] in filter.recipient_ids]
    if filter.account_categories is not None:
        candidates = [
            c for c in candidates if c["account_category_code"] in filter.account_categories
        ]
    if filter.exclude_claim_ids:
        candidates = [c for c in candidates if c["claim_id"] not in filter.exclude_claim_ids]

    if filter.period_start or filter.period_end:
        receipt_ids = {c["receipt_id"] for c in candidates}
        receipts = {
            rid: get_client().collection("receipts").document(rid).get().to_dict()
            for rid in receipt_ids
        }

        def in_period(c: dict) -> bool:
            txn_date = (receipts.get(c["receipt_id"]) or {}).get("transaction_date")
            if txn_date is None:
                return False
            # transaction_date는 Firestore에 ISO 문자열로 저장된다(schema-contract.md
            # §1 예외). filter.period_start/end는 pydantic이 date로 변환한다. 타입이
            # 다르면 비교가 TypeError로 죽으므로 양쪽을 ISO 문자열로 정규화한다.
            # YYYY-MM-DD는 사전순 정렬이 날짜순과 일치해 문자열 비교로 충분하다.
            if isinstance(txn_date, date):
                txn_date = txn_date.isoformat()
            if filter.period_start and txn_date < filter.period_start.isoformat():
                return False
            if filter.period_end and txn_date > filter.period_end.isoformat():
                return False
            return True

        candidates = [c for c in candidates if in_period(c)]

    return candidates
