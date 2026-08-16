"""money-safety.md 한도 — 환경변수, 코드 상수 아님. schema-contract.md §4.

캡 3종은 전부 BASE_CURRENCY minor 단위다. approve와 /payouts 양쪽에서 같은 함수로
검사한다.
"""

import os

from ..payouts.store import get_claims_for_run, get_recipient


def _cap(name: str) -> int | None:
    value = os.environ.get(name, "")
    return int(value) if value else None


def check_caps(run: dict) -> str | None:
    """위반이 있으면 사람이 읽을 사유 문자열을, 없으면 None을 반환한다."""
    claims = get_claims_for_run(run["settlement_run_id"])

    batch_cap = _cap("MAX_AMOUNT_PER_BATCH_MINOR")
    if batch_cap is not None and run["total_amount_minor"] > batch_cap:
        return f"MAX_AMOUNT_PER_BATCH_MINOR exceeded: {run['total_amount_minor']} > {batch_cap}"

    item_cap = _cap("MAX_AMOUNT_PER_ITEM_MINOR")
    if item_cap is not None:
        for claim in claims:
            if claim["amount_minor"] > item_cap:
                return (
                    f"MAX_AMOUNT_PER_ITEM_MINOR exceeded: "
                    f"{claim['claim_id']} {claim['amount_minor']} > {item_cap}"
                )

    monthly_cap = _cap("MAX_AMOUNT_MONTHLY_MINOR")
    if monthly_cap is not None:
        batch_totals: dict[str, int] = {}
        for claim in claims:
            batch_totals[claim["recipient_id"]] = (
                batch_totals.get(claim["recipient_id"], 0) + claim["amount_minor"]
            )
        for recipient_id, batch_amount in batch_totals.items():
            recipient = get_recipient(recipient_id)
            already_paid = recipient["monthly_paid_minor"] if recipient else 0
            projected = already_paid + batch_amount
            if projected > monthly_cap:
                return (
                    f"MAX_AMOUNT_MONTHLY_MINOR exceeded for {recipient_id}: "
                    f"{projected} > {monthly_cap}"
                )

    return None
