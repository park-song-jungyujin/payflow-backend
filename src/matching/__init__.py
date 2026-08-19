"""TODO(B): 결정론적 매칭(금액·날짜윈도우·가맹점명, PayPal 원장 대조) 여기 구현.
지금은 SettlementFilter 조건만 적용하는 TEMP 버전 — 매칭·검증 없음. B가 자리를
비운 동안 A/C의 E2E 테스트가 막히지 않게 하는 placeholder다.
"""

from ..payouts.store import get_client, list_confirmed_claims
from ..schemas.models import SettlementFilter


def select_claims_for_run(filter: SettlementFilter) -> list[dict]:
    candidates = list_confirmed_claims()

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
            if filter.period_start and txn_date < filter.period_start:
                return False
            if filter.period_end and txn_date > filter.period_end:
                return False
            return True

        candidates = [c for c in candidates if in_period(c)]

    return candidates
