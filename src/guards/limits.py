"""money-safety.md 한도 — 환경변수, 코드 상수 아님. schema-contract.md §4.

캡 3종은 전부 PAYOUT_CURRENCY(env) minor 단위다. approve와 /payouts 양쪽에서 같은 함수로
검사한다.
"""

import os
from decimal import Decimal

from ..payouts.currency import convert_minor
from ..payouts.store import get_claims_for_run, get_recipient


def _cap(name: str) -> int | None:
    value = os.environ.get(name, "")
    return int(value) if value else None


def _converted_amount(claim: dict, base_currency: str, fx_rates: dict) -> int:
    currency = claim["currency"]
    if currency == base_currency:
        return claim["amount_minor"]
    rate = Decimal(fx_rates[f"{currency}/{base_currency}"])
    return convert_minor(claim["amount_minor"], currency, base_currency, rate)


def check_caps(run: dict) -> str | None:
    """위반이 있으면 사람이 읽을 사유 문자열을, 없으면 None을 반환한다.

    항목별/월간 캡은 PAYOUT_CURRENCY(env) 환산액 기준이다 — run["fx_rates"]가 이미
    승인 시점에 고정한 환율이라고 가정한다(approve_settlement_run이 check_caps보다
    먼저 계산해서 넣어준다).
    """
    claims = get_claims_for_run(run["settlement_run_id"])
    base_currency = os.environ["PAYOUT_CURRENCY"]
    fx_rates = run.get("fx_rates") or {}

    batch_cap = _cap("MAX_AMOUNT_PER_BATCH_MINOR")
    if batch_cap is not None and run["total_amount_minor"] > batch_cap:
        return f"MAX_AMOUNT_PER_BATCH_MINOR exceeded: {run['total_amount_minor']} > {batch_cap}"

    item_cap = _cap("MAX_AMOUNT_PER_ITEM_MINOR")
    if item_cap is not None:
        for claim in claims:
            converted = _converted_amount(claim, base_currency, fx_rates)
            if converted > item_cap:
                return (
                    f"MAX_AMOUNT_PER_ITEM_MINOR exceeded: "
                    f"{claim['claim_id']} {converted} > {item_cap}"
                )

    monthly_cap = _cap("MAX_AMOUNT_MONTHLY_MINOR")
    if monthly_cap is not None:
        batch_totals: dict[str, int] = {}
        for claim in claims:
            converted = _converted_amount(claim, base_currency, fx_rates)
            batch_totals[claim["recipient_id"]] = (
                batch_totals.get(claim["recipient_id"], 0) + converted
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
