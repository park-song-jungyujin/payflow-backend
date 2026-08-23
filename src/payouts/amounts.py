"""schema-contract.md §4 — recipient별 base_currency 환산 합계.

approve 시점에 `guards/routes.py._lock_fx_and_total`이 고정한 `run["fx_rates"]`를
그대로 쓴다(재조회 없음) — 승인 이후 환율이 바뀌면 approval_amount_hash가 어긋나야
하므로, 여기서 새로 fetch하면 안 된다.
"""

import os
from decimal import Decimal

from .currency import convert_minor
from .store import get_claims_for_run


def per_recipient_amounts(run: dict) -> dict[str, int]:
    """run에 걸린 claims를 recipient_id별로 base_currency 환산해 합산한다."""
    base_currency = os.environ["PAYOUT_CURRENCY"]
    fx_rates = run.get("fx_rates") or {}

    totals: dict[str, int] = {}
    for claim in get_claims_for_run(run["settlement_run_id"]):
        currency = claim["currency"]
        if currency == base_currency:
            converted = claim["amount_minor"]
        else:
            rate = Decimal(fx_rates[f"{currency}/{base_currency}"])
            converted = convert_minor(claim["amount_minor"], currency, base_currency, rate)
        recipient_id = claim["recipient_id"]
        totals[recipient_id] = totals.get(recipient_id, 0) + converted
    return totals
